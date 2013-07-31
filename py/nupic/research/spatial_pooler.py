# ----------------------------------------------------------------------
# Numenta Platform for Intelligent Computing (NuPIC)
# Copyright (C) 2013, Numenta, Inc.  Unless you have purchased from
# Numenta, Inc. a separate commercial license for this software code, the
# following terms and conditions apply:
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see http://www.gnu.org/licenses.
#
# http://numenta.org/licenses/
# ----------------------------------------------------------------------

import copy
import cPickle
import itertools
import numpy

from nupic.bindings.math import SM32 as SparseMatrix, \
																SM_01_32_32 as SparseBinaryMatrix, \
																count_gte, GetNTAReal
from nupic.bindings.algorithms import Inhibition2 as Inhibition, \
																			cpp_overlap, cpp_overlap_sbm
from nupic.bindings.algorithms import adjustMasterValidPermanence
from nupic.bindings.math import Random as NupicRandom
from nupic.math.cross import cross
from operator import itemgetter
import nupic.research.fdrutilities as fdru

realDType = GetNTAReal()



class SpatialPooler(object):
	"""
	This class implements a the spatial pooler. It is in charge of handling the relationships 
	between the columns of a region and the inputs bits. The primary public interface to this
	function is the "compute" method, which takes in an input vector and returns a list of 
	activeColumns columns. 
	Example Usage:
	>
	> sp = SpatialPooler(...)
	> for line in file:
	>   inputVector = numpy.array(line)
	>   activeColumns, anomaly = sp.compute(inputVector)
	>   ...
	"""
	def __init__(self,
							 inputDimensions=[32,32],
							 columnDimensions=[64,64],
							 potentialRadius=16,
							 potentialPct=0.5,
							 globalInhibition=False,
							 localAreaDensity=-1.0,
							 numActiveColumnsPerInhArea=10.0,
							 stimulusThreshold=0,
							 synPermInactiveDec=0.01,
							 synPermActiveInc=0.1,
							 synPermActiveSharedDec=0.0,
							 synPermOrphanDec=0.0,
							 synPermConnected=0.10,
							 minPctOverlapDutyCycle=0.001,
							 minPctActiveDutyCycle=0.001,
							 dutyCyclePeriod=1000,
							 maxBoost=10.0,
							 seed=-1,
							 spVerbosity=0
							 ):
		"""
		Parameters:
		----------------------------
		inputDimensions:      A number, list or numpy array representing the 
													dimensions of the input vector. Format is [height, 
													width, depth, ...], where each value represents the 
													size of the dimension. For a topology of one dimesion 
													with 100 inputs use 100, or [100]. For a two 
													dimensional topology of 10x5 use [10,5]. 
		columnDimensions:     A number, list or numpy array representing the 
													dimensions of the columns in the region. Format is 
													[height, width, depth, ...], where each value 
													represents the size of the dimension. For a topology 
													of one dimesion with 2000 columns use 2000, or 
													[2000]. For a three dimensional topology of 32x64x16 
													use [32, 64, 16]. 
		potentialRadius:      This parameter deteremines the extent of the input 
													that each column can potentially be connected to. 
													This can be thought of as the input bits that
													are visible to each column, or a 'receptiveField' of 
													the field of vision. A large enough value will result 
													in the 'global coverage', meaning that each column 
													can potentially be connected to every input bit. This 
													parameter defines a square (or hyper square) area: a 
													column will have a max square potential pool with 
													sides of length 2 * potentialRadius + 1. 
		potentialPct:         The percent of the inputs, within a column's
				 									potential radius, that a column can be connected to. 
				 									If set to 1, the column will be connected to every 
				 									input within its potential radius. This parameter is 
				 									used to give each column a unique potential pool when 
				 									a large potentialRadius causes overlap between the 
				 									columns. At initialization time we choose 
				 									((2*potentialRadius + 1)^(# inputDimensions) * 
				 									potentialPct) input bits to comprise the column's
				 									potential pool.
		globalInhibition:     If true, then during inhibition phase the winning 
													columns are selected as the most active columns from 
													the region as a whole. Otherwise, the winning columns 
													are selected with resepct to their local neighborhood
		localAreaDensity:     The desired density of active columns within a local
													inhibition area (the size of which is set by the
													internally calculated inhibitionRadius, which is in
													turn determined from the average size of the 
													connected potential pools of all columns). The 
													inhibition logic will insure that at most N columns 
													remain ON within a local inhibition area, where N = 
													localAreaDensity * (total number of columns in 
													inhibition area).
		numActivePerInhArea:  An alternate way to control the density of the active
													columns. If numActivePerInhArea is specified then
													localAreaDensity must be -1, and vice versa. When
													using numActivePerInhArea, the inhibition logic will
													insure that at most 'numActivePerInhArea' columns
													remain ON within a local inhibition area (the size of
													which is set by the internally calculated
													inhibitionRadius, which is in turn determined from 
													the average size of the connected receptive fields of 
													all columns). When using this method, as columns 
													learn and grow their effective receptive fields, the
													inhibitionRadius will grow, and hence the net density
													of the active columns will *decrease*. This is in
													contrast to the localAreaDensity method, which keeps
													the density of active columns the same regardless of
													the size of their receptive fields.
		stimulusThreshold:    This is a number specifying the minimum number of
													synapses that must be on in order for a columns to
													turn ON. The purpose of this is to prevent noise 
													input from activating columns. Specified as a percent 
													of a fully grown synapse.
		synPermInactiveDec:   The amount by which an inactive synapse is 
													decremented in each round. Specified as a percent of 
													a fully grown synapse.
		synPermActiveInc:     The amount by which an active synapse is incremented 
													in each round. Specified as a percent of a
													fully grown synapse.
		synPermActiveSharedDec: The amount by which to decrease the permanence of 
													an active synapse which is connected to another 
													column that is active at the same time. Specified as 
													a percent of a fully grown synapse.
		synPermOrphanDec:     The amount by which to decrease the permanence of an 
													active synapse on a column which has high overlap 
													with the input, but was inhibited (an "orphan" 
													column).
		synPermConnected:     The default connected threshold. Any synapse whose
													permanence value is above the connected threshold is
													a "connected synapse", meaning it can contribute to
													the cell's firing.
		minPctOvlerapDutyCycle: A number between 0 and 1.0, used to set a floor on
													how often a column should have at least
													stimulusThreshold active inputs. Periodically, each
													column looks at the overlap duty cycle of
													all other column within its inhibition radius and 
													sets its own internal minimal acceptable duty cycle 
													to: minPctDutyCycleBeforeInh * max(other columns' 
													duty cycles).
													On each iteration, any column whose overlap duty 
													cycle falls below this computed value will  get
													all of its permanence values boosted up by
													synPermActiveInc. Raising all permanences in response
													to a sub-par duty cycle before  inhibition allows a
													cell to search for new inputs when either its
													previously learned inputs are no longer ever active,
													or when the vast majority of them have been 
													"hijacked" by other columns.
		minPctActiveDutyCycle: A number between 0 and 1.0, used to set a floor on
													how often a column should be activate.
													Periodically, each column looks at the activity duty 
													cycle of all other columns within its inhibition 
													radius and sets its own internal minimal acceptable 
													duty cycle to:
														minPctDutyCycleAfterInh *
														max(other columns' duty cycles).
													On each iteration, any column whose duty cycle after
													inhibition falls below this computed value will get
													its internal boost factor increased.
		dutyCyclePeriod:      The period used to calculate duty cycles. Higher
													values make it take longer to respond to changes in
													boost or synPerConnectedCell. Shorter values make it
													more unstable and likely to oscillate.
		 maxBoost:            The maximum overlap boost factor. Each column's
													overlap gets multiplied by a boost factor
													before it gets considered for inhibition.
													The actual boost factor for a column is number 
													between 1.0 and maxBoost. A boost factor of 1.0 is 
													used if the duty cycle is >= minOverlapDutyCycle, 
													maxBoost is used if the duty cycle is 0, and any duty 
													cycle in between is linearly extrapolated from these 
													2 endpoints.
		seed:                 Seed for our own pseudo-random number generator.
		spVerbosity:          spVerbosity level: 0, 1, 2, or 3
		"""

		# Verify input is valid
		inputDimensions = numpy.array(inputDimensions)
		columnDimensions = numpy.array(columnDimensions)
		numColumns = columnDimensions.prod()
		numInputs = inputDimensions.prod()

		assert(numColumns > 0)
		assert(numInputs > 0)
		assert (numActiveColumnsPerInhArea > 0 or localAreaDensity > 0)
		assert (localAreaDensity == -1 or 
						(localAreaDensity >0 and localAreaDensity < 1))


		# save arguments
		self._numInputs = numInputs
		self._numColumns = numColumns
		self._columnDimensions = columnDimensions
		self._inputDimensions = inputDimensions
		self._potentialRadius = min(potentialRadius, numInputs)    
		self._potentialPct = potentialPct
		self._globalInhibition = globalInhibition
		self._numActiveColumnsPerInhArea = numActiveColumnsPerInhArea
		self._localAreaDensity = localAreaDensity
		self._stimulusThreshold = stimulusThreshold
		self._synPermInactiveDec = synPermInactiveDec
		self._synPermActiveInc = synPermActiveInc
		self._synPermActiveSharedDec = synPermActiveSharedDec
		self._synPermBelowStimulusInc = synPermConnected / 10.0
		self._synPermOrphanDec = synPermOrphanDec
		self._synPermConnected = synPermConnected
		self._minPctOverlapDutyCycles = minPctOverlapDutyCycle
		self._minPctActiveDutyCycles = minPctActiveDutyCycle
		self._dutyCyclePeriod = dutyCyclePeriod
		self._maxBoost = maxBoost
		self._spVerbosity = spVerbosity

		# Extra parameter settings
		self._synPermMin = 0.0
		self._synPermMax = 1.0
		self._synPermTrimThreshold = synPermActiveInc / 2.0
		self._updatePeriod = 50

		# Internal state
		self._version = 1.0
		self._iterationNum = 0
		self._iterationLearnNum = 0

		# Store the set of all inputs that are within each column's potential pool.
		# 'potentialPools' is a matrix, whose rows represent cortical columns, and 
		# whose columns represent the input bits. if potentialPools[i][j] == 1,
		# then input bit 'j' is in column 'i's potential pool. A column can only be 
		# connected to inputs in its potential poolThe indices refer to a 
		# falttenned version of both the inputs and columns. Namely, irrespective 
		# of the topology of the inputs and columns, they are treated as being a 
		# one dimensional array. Since a column is typically connected to only a 
		# subset of the inputs, many of the entries in the matrix are 0. Therefore 
		# the the potentialPool matrix is stored using the SparseBinaryMatrix 
		# class, to reduce memory footprint and compuation time of algorithms that 
		# require iterating over the data strcuture.
		potentialPools = [self._mapPotential(i) for i in xrange(numColumns)]
		self._potentialPools = SparseBinaryMatrix(potentialPools)

		# Initialize the permanences for each column. Similar to the 
		# 'self._potentialPools', the permances are stored in a matrix whose rows
		# represent the cortial columns, and whose columns represent the input 
		# bits. if self._permanences[i][j] = 0.2, then the synapse connecting 
		# cortical column 'i' to input bit 'j'  has a permanence of 0.2. Here we 
		# also use the SparseMatrix class to reduce the memory footprint and 
		# computation time of algorithms that require iterating over the data 
		# structure. 
		self._permanences = SparseMatrix(numColumns, numInputs)

		# 'self._connectedSynapses' is a similar matrix to 'self._permanences' 
		# (rows represent cortial columns, columns represent input bits) whose 
		# entries represent whether the cortial column is connected to the input 
		# bit, i.e. its permanence value is greater than 'synPermConnected'. While 
		# this information is readily available from the 'self._permanence' matrix, 
		# it is stored separately for efficiency purposes.
		self._connectedSynapses = SparseBinaryMatrix(numInputs)
		self._connectedSynapses.resize(numColumns, numInputs)

		# Stores the number of connected synapses for each column. This is simply
		# a sum of each row of 'self._connectedSynapses'. again, while this 
		# information is readily available from 'self._connectedSynapses', it is
		# stored separately for efficiency purposes.
		self._connectedCounts = numpy.zeros(numColumns)
		for i in xrange(numColumns):
			self._updatePermanencesForColumn(self._initPermanence(i), i) 
		
		# Ensure that each column is connected to enough input bits such that it 
		# has a chance of being activated.
		self._raisePermanenceToThreshold()

		self._overlapDutyCycles = numpy.zeros(numColumns)
		self._activeDutyCycles = numpy.zeros(numColumns)
		self._minOverlapDutyCycles = numpy.zeros(numColumns) + 1e-6
		self._minActiveDutyCycles = numpy.zeros(numColumns) + 1e-6
		self._boostFactors = numpy.ones(numColumns)

		# The inhibition radius determines the size of a column's local 
		# neighborhood. of a column. A cortical column must overcome the overlap 
		# score of columns in his neighborhood in order to become actives. This 
		# radius is updated every learning round. It grows and shrinks with the 
		# average number of connected synapses per column.
		self._inhibitionRadius = 0
		self._updateInhibitionRadius()

		self._seed(seed)
	

	def compute(self, inputVector, learn=True):
		"""
		This is the primary public method of the SpatialPooler class. This 
		function takes a input vector and outputs the indices of the active columns 
		along with the anomaly score for the that input. If 'learn' is set to True,
		this method also updates the permanences of the columns.

		Parameters:
		----------------------------
		inputVector:    a numpy array of 0's and 1's thata comprises the input to 
										the spatial pooler. The array will be treated as a one
										dimensional array, therefore the dimensions of the array
										do not have to much the exact dimensions specified in the 
										class constructor. In fact, even a list would suffice. 
										The number of input bits in the vector must, however, 
										match the number of bits specified by the call to the 
										constructor. Therefore there must be a '0' or '1' in the
										array for every input bit.
		learn:					a boolean value indicating whether learning should be 
										performed. Learning entails updating the  permanence 
										values of the synapses, and hence modifying the 'state' 
										of the model. setting learning to 'off' might be useful
										for indicating separate training vs. testing sets. 
		"""

		assert (numpy.size(inputVector) == self._numInputs)
		inputVector = numpy.array(inputVector, dtype=realDType)
		inputVector.reshape(-1)
		overlaps, overlapsPct = self._calculateOverlap(inputVector)

		if learn:
			boostedOverlaps = self._boostFactors * overlaps
		else:
			boostedOverlaps = overlaps

		activeColumns = self._inhibitColumns(boostedOverlaps)
		anomalyScore = self._calculateAnomalyScore(overlaps, activeColumns)

		# if not learn: - don't let columns that never learned win!
		# TODO: <implement this>

		if learn:
			orphanColumns = self._calculateOrphanColumns(activeColumns, overlapsPct)
			sharedInputs = self._calculateSharedInputs(inputVector, activeColumns)
			self._adaptSynapses(inputVector, sharedInputs, orphanColumns)
			self._raisePermanenceToThreshold()
			self._bumpUpWeakColumns() 
			self._updateBoostFactors()
			self._updateDutyCycles(overlaps, activeColumns)
			self._updateInhibitionRadius()

			if self._isUpdateRound():
				self._updateMinDutyCycles()

		self._updateBookeeping(learn)

		return numpy.array(activeColumns), anomalyScore



	def _calculateAnomalyScore(self, overlaps, activeColumns):
		"""
		The anomaly score is a value between 0 and 1 measuring the familiarity of 
		the input vector to input vectors that were witnessed in the past. Since 
		columns become 'connected' to input bits which turn on repeatedly once a 
		column is active, a high overlap in active columns is a measure of 
		similarity between the current input, and inputs that have activated them 
		in the past. Furthermore, since some columns become active more frequently 
		than others, having an infrequently active column become active is another 
		indication that the input is anomalous. We take these two considerations 
		into account in computing the anomaly score.

		Parameters:
		----------------------------
		overlaps:				an array containing the overlap score for each  column. 
										The overlap score for a column is defined as the number 
										of synapses in a "connected state" (connected synapses) 
										that are connected to input bits which are turned on.
		activeColumns:	An array containing the indices of the active columns, 
										the sprase set of columns which survived inhibition
		"""
		if activeColumns.size == 0:
			return 1.0
		anomalyScores = overlaps[activeColumns]
		anomalyScores *= self._activeDutyCycles[activeColumns]
		return 1.0 / (numpy.sum(anomalyScores) + 1)


	def _updateMinDutyCycles(self):
		"""
		Updates the minimum duty cycles defining normal activity for a column. A
		column with activity duty cycle below this minimum threshold is boosted.
		"""
		if self._globalInhibition or self._inhibitionRadius > self._numInputs:
			self._updateMinDutyCyclesGlobal()
		else:
			self._updateMinDutyCyclesLocal()


	def _updateMinDutyCyclesGlobal(self):
		"""
		Updates the minimum duty cycles in a global fashion. Sets the minimum duty
		cycles for the overlap and activation of all columns to be a percent of the 
		maximum in the region, specified by minPctOverlapDutyCycle and 
		minPctActiveDutyCycle respectively. Functionaly it is equivalent to 
		_updateMinDutyCyclesLocal, but this function exploits the globalilty of the
		compuation to perform it in a straightforward, and more efficient manner.
		"""
		self._minOverlapDutyCycles.fill(
				self._minPctOverlapDutyCycles * self._overlapDutyCycles.max()
			)
		self._minActiveDutyCycles.fill(
				self._minPctActiveDutyCycles * self._activeDutyCycles.max()
			)


	def _updateMinDutyCyclesLocal(self):
		"""
		Updates the minimum duty cycles. The minimum duty cycles are determined 
		locally. Each column's minimum duty cycles are set to be a percent of the
		maximum duty cycles in the column's neighborhood. Unlike 
		_updateMinDutyCyclesGlobal, here the values can be quite different for
		different columns.
		"""
		for i in xrange(self._numColumns):
			maskNeighbors = self._getNeighborsND(i, self._columnDimensions,
				self._inhibitionRadius)
			self._minOverlapDutyCycles[i] = (
				self._overlapDutyCycles[maskNeighbors].max() * 
				self._minPctOverlapDutyCycles
			)
			self._minActiveDutyCycles[i] = (
				self._activeDutyCycles[maskNeighbors].max() * 
				self._minPctActiveDutyCycles
			)


	def _updateDutyCycles(self, overlaps, activeColumns):
		"""
		Updates the duty cycles for each column. The OVERLAP duty cycle is a moving
		average of the number of inputs which overlapped with the each column. The
		ACTIVITY duty cycles is a moving average of the frequency of activation for 
		each column.

		Parameters:
		----------------------------
		overlaps:				an array containing the overlap score for each column. 
										The overlap score for a column is defined as the number 
										of synapses in a "connected state" (connected synapses) 
										that are connected to input bits which are turned on.
		activeColumns:	An array containing the indices of the active columns, 
										the sprase set of columns which survived inhibition
		"""
		activeArray = numpy.zeros(self._numColumns)
		if activeColumns.size > 0:
			activeArray[activeColumns] = 1
		period = self._dutyCyclePeriod
		if (period < self._iterationNum):
			period = self._iterationNum

		self._overlapDutyCycles = self._updateDutyCyclesHelper(
																self._overlapDutyCycles, 
																overlaps, 
																period
															)

		self._activeDutyCycles = self._updateDutyCyclesHelper(
																self._activeDutyCycles, 
																activeArray,
																period
															)



	def _updateInhibitionRadius(self):
		"""
		Update the inhibition radius. The inhibition radius is a meausre of the
		square (or hypersquare) of columns that each a column is "conencted to"
		on average. Since columns are are not connected to each other directly, we 
		determine this quantity by first figuring out how many *inputs* a column is 
		connected to, and then multiplying it by the total number of columns that 
		exist for each input. For multiple dimension the aforementioned 
		calculations are averaged over all dimensions of inputs and columns. This 
		value is meaningless if global inhibition is enabled.
		"""
		if self._globalInhibition:
			self._inhibitionRadius = self._numColumns
			return

		avgConnectedSpan = numpy.average( 
													[self._avgConnectedSpanForColumn1D(i)
													for i in xrange(self._numColumns)]
												)
		columnsPerInput = self._avgColumnsPerInput()
		newInhibitionRadius = avgConnectedSpan * columnsPerInput
		newInhibitionRadius = max(1.0, newInhibitionRadius)
		self._inhibitionRadius = int(round(newInhibitionRadius))


	def _avgColumnsPerInput(self):
		"""
		The average number of columns per input, taking into account the topology 
		of the inputs and columns. This value is used to calculate the inhibition 
		radius. This function supports an arbitrary number of 
		dimensions, but the number of dimensions must be the same for both the
		inputs and columns
		"""
		#TODO: extend to support different number of dimensions for inputs and 
		# columns
		columnsPerInput = (self._columnDimensions.astype(realDType) /
				self._inputDimensions
			)
				 # (self._inputDimensions - self._inputBorder)
		return numpy.average(columnsPerInput)


	def _avgConnectedSpanForColumn1D(self, index):
		"""
		The range of connected synapses for column. This is used to 
		calculate the inhibition radius. This variation of the function only 
		supports a 1 dimensional column toplogy.

		Parameters:
		----------------------------
		index:					The index identifying a column in the permanence, potential 
										and connectivity matrices,
		"""
		assert(self._inputDimensions.size == 1)
		connected = self._connectedSynapses.getRow(index).nonzero()[0]
		if connected.size == 0:
			return 0
		else:
			return max(connected) - min(connected)


	def _avgConnectedSpanForColumn2D(self, index):
		"""
		The range of connectedSynapses per column, averaged for each dimension. 
		This vaule is used to calculate the inhibition radius. This variation of 
		the  function only supports a 2 dimensional column topology.

		Parameters:
		----------------------------
		index:					The index identifying a column in the permanence, potential 
										and connectivity matrices,
		"""
		assert(self._inputDimensions.size == 2)
		connected = self._connectedSynapses.getRow(index)
		(rows, cols) = connected.reshape(self._inputDimensions).nonzero()
		if  rows.size == 0 and cols.size == 0:
			return 0
		rowSpan = rows.max() - rows.min()
		colSpan = cols.max() - cols.min()
		return numpy.average([rowSpan, colSpan])


	def _avgConnectedSpanForColumnND(self, index):
		"""
		The range of connectedSynapses per column, averaged for each dimension. 
		This vaule is used to calculate the inhibition radius. This variation of 
		the function only supports arbitrary column dimensions.

		Parameters:
		----------------------------
		index:					The index identifying a column in the permanence, potential 
										and connectivity matrices.
		"""
		dimensions = self._inputDimensions
		bounds = numpy.cumprod(numpy.append([1], dimensions[::-1][:-1]))[::-1]
		def toCoords(index):
			return (index / bounds) % dimensions

		connected = self._connectedSynapses.getRow(index).nonzero()[0]
		if connected.size == 0:
			return 0
		maxCoord = numpy.empty(self._inputDimensions.size)
		minCoord = numpy.empty(self._inputDimensions.size)
		maxCoord.fill(-1)
		minCoord.fill(max(self._inputDimensions))
		for i in connected:
			maxCoord = numpy.maximum(maxCoord, toCoords(i))
			minCoord = numpy.minimum(minCoord, toCoords(i))
		return numpy.average(maxCoord - minCoord)


	def _adaptSynapses(self, inputVector, sharedInputs, orphanColumns):
		"""
		The primary method in charge of learning. Adapts the permanence values of 
		the synapses based on the input vector, and the chosen columns after 
		inhibition round. Permanence values for synapses connected to input bits
		that are turned on, and decreased for synapses connected to inputs bits 
		that are turned off. Shared inputs, which are turned on input bits that are 
		connected to more than one active column, and orphan columns, which are
		columns with 100 percent overlap  yet did not survive inhibition, are 
		treated slightly differently.

		Parameters:
		----------------------------
		inputVector:    a numpy array of 0's and 1's thata comprises the input to 
										the spatial pooler. There exists an entry in the array 
										for every input bit.
		sharedInputs:		an array containing the indices of the input bits that 
										happen to be connected to more than one active column
		orphanColumns:	an array containing the indices of the columns that are
										orhpans.
		"""
		inputIndices = numpy.where(inputVector > 0)[0]
		orphanSet = set(orphanColumns)
		permChanges = numpy.zeros(self._numInputs)
		permChanges.fill(-1 * self._synPermInactiveDec)
		permChanges[inputIndices] = self._synPermActiveInc
		permChanges[sharedInputs] -= self._synPermActiveSharedDec
		for i in xrange(self._numColumns):
			perm = self._permanences.getRow(i)
			maskRF = numpy.where(self._potentialPools.getRow(i) > 0)[0]
			perm[maskRF] += permChanges[maskRF]
			if i in orphanSet:
				perm[maskRF] -= self._synPermOrphanDec
			self._updatePermanencesForColumn(perm, i)


	def _bumpUpWeakColumns(self):
		"""
		This method increases the permanence values of synapses of columns whose 
		activity level has been too low. Such columns are identified by having an
		overlap duty cycle that drops too much below those of their peers. The
		permanence values for such columns are increased.
		"""
		weakColumns = numpy.where(self._overlapDutyCycles
																< self._minOverlapDutyCycles)[0]   
		for i in weakColumns:
			perm = self._permanences.getRow(i).astype(realDType)
			maskRF = numpy.where(self._potentialPools.getRow(i) > 0)[0]
			perm[maskRF] += self._synPermBelowStimulusInc
			self._updatePermanencesForColumn(perm, i)
	 

	def _raisePermanenceToThreshold(self):
		"""
		This method ensures that each column has enough connections to input bits
		to allow it to become active. Since a column must have at least 
		'self._stimulusThreshold' overlaps in order to be considered during the 
		inhibition phase, columns without such minimal number of connections, even
		if all the input bits they are connected to turn on, have no chance of 
		obtaining the minimum threshold. For such columns, the permanence values
		are increased until the minimum number of connections are formed.
		"""
		belowThreshold = numpy.where(
			self._connectedCounts < self._stimulusThreshold)[0]
		for i in belowThreshold:
			perm = self._permanences.getRow(i).astype(realDType)
			maskRF = numpy.where(self._potentialPools.getRow(i) > 0)[0]
			while True:
				perm[maskRF] += self._synPermBelowStimulusInc
				numConnected = count_gte(perm, self._synPermConnected)
				if numConnected >= self._stimulusThreshold:
					break
			self._updatePermanencesForColumn(perm, i)


	def _updatePermanencesForColumn(self, perm, index):
		"""
		This method updates the permanence matrix with a column's new permanence
		values. The column is identified by its index, which reflects the row in
		the matrix, and the permanence is given in 'dense' form, i.e. a full 
		arrray containing all the zeros as well as the non-zero values. It is in 
		charge of implementing 'clipping' - ensuring that the permanence values are
		always between 0 and 1 - and 'trimming' - enforcing sparsity by zeroing out
		all permanence values below '_synPermTrimThreshold'. It also maintains
		the consistency between 'self._permanences' (the matrix storeing the 
		permanence values), 'self._connectedSynapses', (the matrix storing the bits
		each column is connected to), and 'self._connectedCounts' (an array storing
		the number of input bits each column is connected to). Every method wishing 
		to modify the permanence matrix should do so through this method.

		Parameters:
		----------------------------
		perm:						An array of permanence values for a column. The array is 
										"dense", i.e. it contains an entry for each input bit, even
										if the permanence value is 0.
		index:					The index identifying a column in the permanence, potential 
										and connectivity matrices,
		"""
		numpy.clip(perm,self._synPermMin, self._synPermMax, out=perm)
		perm[perm < self._synPermTrimThreshold] = 0
		newConnected = numpy.where(perm > self._synPermConnected)[0]
		self._permanences.setRowFromDense(index, perm)
		self._connectedSynapses.replaceSparseRow(index, newConnected)
		self._connectedCounts[index] = newConnected.size


	def _initPermanence(self, index):
		"""
		Initializes the permanences of a column. The method
		returns a 1-D array the size of the input, where each entry in the
		array represents the initial permanence value between the input bit
		at the particular index in the array, and the column represented by
		the 'index' parameter.

		Parameters:
		----------------------------
		index:					The index identifying a column in the permanence, potential 
										and connectivity matrices,
		"""
		# Determine which inputs bits will start out as connected
		# to the inputs. Initially a subset of the input bits in a 
		# column's potential pool will be connected. This number is
		# given by the parameter "potentialPct"
		rand = numpy.random.random(2*self._potentialRadius+1)
		threshold = 1-self._potentialPct
		connectedSynapses = numpy.where(rand > threshold)[0]
		unconnectedSynpases = list(set(range(self._potentialRadius)) -
			set(connectedSynapses))
		maxPermValue = min(1.0, self._synPermConnected +
								self._synPermInactiveDec)

		# All connected synapses are to have a permanence value between
		# synPermConnected and synPermActiveInc/4
		connectedPermRange = self._synPermActiveInc / 4
		connectedPermOffset = self._synPermConnected

		# All unconnected synapses are to have a permanence value 
		# between 0 and synPermConnected
		unconnectedPermRange = self._synPermConnected
		unconnectedPermOffset = 0

		# Create a vector to contain only the permanence values inside
		# a column's potential pool, and fill it with random values
		# from the aforementioned distributions
		permRF = numpy.zeros(2*self._potentialRadius+1)
		permRF[connectedSynapses] = (
				numpy.random.random(len(connectedSynapses))
				* connectedPermRange + connectedPermOffset
			)
		permRF[unconnectedSynpases] = (
				numpy.random.random(len(unconnectedSynpases))
				* unconnectedPermRange + unconnectedPermOffset
			)

		# Clip off low values. Since we use a sparse representation
		# to store the permanence values this helps reduce memory
		# requirements.
		permRF[permRF < self._synPermTrimThreshold] = 0

		# Create a full vector the size of the entire input and fill in
		# the permanence values we just computed at the correct indices
		maskRF = numpy.where(self._mapPotential(index) > 0)[0]
		permanences = numpy.zeros(self._numInputs)
		permanences[maskRF] = permRF

		return permanences


	def _mapPotential(self, index):
		"""
		Maps a column to its input bits. This method encapsultes the topology of 
		the region. It takes the index of the column as an argument and determines 
		what are the indices of the input vector that are located within the 
		column's potential pool. The return value is a list containing the indices 
		of the input bits. The current implementation of the base class only 
		supports a 1 dimensional topology of columsn with a 1 dimensional topology 
		of inputs. To extend this class to support 2-D topology you will need to 
		override this method. Examples of the expected output of this method:
		* If the potentialRadius is greater than or equal to the entire input 
			space, (global visibility), then this method returns an array filled with 
			all the indices
		* If the topology is one dimensional, and the potentialRadius is 5, this 
			method will return an array containing 5 consecutive values centered on 
			the index of the column (wrapping around if necessary).
		* If the topology is two dimensional (not implemented), and the 
			potentialRadius is 5, the method should return an array containing 25 
			'1's, where the exact indices are to be determined by the mapping from 
			1-D index to 2-D position.

		Parameters:
		----------------------------
		index:					The index identifying a column in the permanence, potential 
										and connectivity matrices,
		"""
		indices = numpy.array(range(2*self._potentialRadius+1))
		indices += index
		indices -= self._potentialRadius
		indices %= self._numInputs
		indices = list(set(indices))
		mask = numpy.zeros(self._numInputs)
		mask[indices] = 1
		return mask


	@staticmethod
	def _updateDutyCyclesHelper(dutyCycles, newInput, period):
		"""
		Updates a duty cycle estimate with a new value. This is a helper
		function that is used to update several duty cycle variables in 
		the Column class, such as: overlapDutyCucle, activeDutyCycle,
		minPctDutyCycleBeforeInh, minPctDutyCycleAfterInh, etc. returns
		the updated duty cycle. Duty cycles are updated according to the following 
		formula:

									(period - 1)*dutyCycle + newValue
			dutyCycle := ----------------------------------
															period

		Parameters:
		----------------------------
		dutyCycles:			An array containing one or more duty cycle values that need
										to be updated
		newInput:				A new numerical value used to update the duty cycle
		period:					The period of the duty cycle			
		"""
		assert(period >= 1)
		return (dutyCycles * (period -1.0) + newInput) / period


	def _updateBoostFactors(self):
		"""
		Update the boost factors for all columns. The boost factors are used to 
		artificially increase the overlap of inactive columns to increase their 
		chances of becoming active, and hence encourage participation of more 
		columns in the learning process. This is a line defined as: y = mx + b
		boost = (1-maxBoost)/minDuty * dutyCycle + maxFiringBoost. Intuitively this
		means that columns that have been active enough have a boost factor of 1,
		meaning their overlap is not boosted. Columns whose active duty cycle drops
		too much below that of their neighbors are boosted depending on how 
		infrequently they have been active. The more infrequent, the more they are 
		boosted. The exact boost factor is linearly interpolated between the points 
		(dutyCycle:0, boost:maxFiringBoost) and (dutyCycle:minDuty, boost:1.0). 

						boostFactor
								^
		maxBoost _  |
								|\
								| \
					1  _  |  \ _ _ _ _ _ _ _
								|   
								+--------------------> activeDutyCycle
									 |
						minActiveDutyCucle
		"""
		
		self._boostFactors = ((1 - self._maxBoost) / self._minActiveDutyCycles * 
				self._activeDutyCycles) + self._maxBoost

		self._boostFactors[self._activeDutyCycles > 
			self._minActiveDutyCycles] = 1.0


	def _updateBookeeping(self, learn):
		"""
		Updates counters instance variables each round.

		Parameters:
		----------------------------
		learn:					a boolean value indicating whether learning should be 
										performed. Learning entails updating the  permanence 
										values of the synapses, and hence modifying the 'state' 
										of the model. setting learning to 'off' might be useful
										for indicating separate training vs. testing sets. 
		"""
		self._iterationNum += 1
		if learn:
			self._iterationLearnNum += 1


	def _calculateOverlap(self, inputVector):
		"""
		This function determines each column's overlap with the current input 
		vector. The overlap of a column is the number of synapses for that column
		that are connected (permance value is greater than '_synPermConnected') 
		to input bits which are turned on. The implementation takes advandage of 
		the SpraseBinaryMatrix class to perform this calculation efficiently.

		Parameters:
		----------------------------
		inputVector:    a numpy array of 0's and 1's thata comprises the input to 
										the spatial pooler. There exists an entry in the array 
										for every input bit.		
		"""
		overlaps = numpy.zeros(self._numColumns).astype(realDType)
		self._connectedSynapses.rightVecSumAtNZ_fast(inputVector, overlaps)
		overlapsPct = overlaps.astype(realDType) / self._connectedCounts
		return overlaps, overlapsPct


	def _calculateOrphanColumns(self, activeColumns, overlapsPct):
		"""
		Determine the orphan columns for a given set of active columns and 
		overlaps. Orphan columns are defined as columns with 100% overlap of the 
		input vector, meaning each one of their connected synapses was connected to 
		an input bit which was turned on, yet the columns did not survive the 
		inhibition round. Essentially these are columns who have learned a 
		particular input pattern, but there  exists other columns who have learned 
		a better representation of that same pattern.

		Parameters:
		----------------------------
		activeColumns:	An array containing the indices of the active columns, 
										the sprase set of columns which survived inhibition
		overlapsPct:		An array containing the overlap percent for each column, a 
										continuous value between 0 and 1. There exists an entry in 
										the array for every column
		"""
		perfectOverlaps = set(numpy.where(overlapsPct >= 1)[0])
		return list(perfectOverlaps.intersection(set(activeColumns)))


	def _calculateSharedInputs(self, inputVector, activeColumns):
		"""
		Determines the shared inputs for a set of active columns and an input 
		vector. Shared inputs are defined as input bits which are connected to two  
		or more active columns.

		Parameters:
		----------------------------
		inputVector:    a numpy array of 0's and 1's thata comprises the input to 
										the spatial pooler. There exists an entry in the array 
										for every input bit.
		activeColumns:	An array containing the indices of the active columns, 
										the sprase set of columns which survived inhibition
		"""
		connectedSynapses = SparseMatrix(self._connectedSynapses)
		inputCoverage = connectedSynapses.addListOfRows(activeColumns)
		sharedInputs = numpy.where(numpy.logical_and(inputCoverage > 1,
			inputVector > 0))[0]
		return sharedInputs


	def _inhibitColumns(self, overlaps):
		"""
		Performs inhibition. This method calculates the necessary values needed to
		actually perform inhibition and then delegates the task of picking the 
		active columns to helper functions.

		Parameters:
		----------------------------
		overlaps:				an array containing the overlap score for each  column. 
										The overlap score for a column is defined as the number 
										of synapses in a "connected state" (connected synapses) 
										that are connected to input bits which are turned on.
		"""
		# determine how many columns should be selected in the inhibition phase. 
		# This can be specified by either setting the 'numActiveColumnsPerInhArea' 
		# parameter of the 'localAreaDensity' parameter when initializing the class
		overlaps = overlaps.copy()
		if (self._numActiveColumnsPerInhArea > 0):
			numActive = self._numActiveColumnsPerInhArea
		else:
			inhibitionArea = ((2*self._inhibitionRadius + 1) 
																		** self._columnDimensions.size)
			inhibitionArea = min(self._numColumns, inhibitionArea)
			numActive = int(round(self._localAreaDensity * inhibitionArea))

		# Add a little bit of random noise to the scores to help break
		# ties.
		tieBreaker = 0.1*numpy.random.rand(self._numColumns)
		overlaps += tieBreaker

		if self._globalInhibition or \
			self._inhibitionRadius > max(self._columnDimensions):
			return self._inhibitColumnsGlobal(overlaps, numActive)
		else:
			return self._inhibitColumnsLocal(overlaps, numActive)

	
	def _inhibitColumnsGlobal(self, overlaps, numActive):
		"""
		Perform global inhibition. Performing global inhibition entails picking the 
		top 'numActive' columns with the highest overlap score in the entire 
		region.

		Parameters:
		----------------------------
		overlaps:				an array containing the overlap score for each  column. 
										The overlap score for a column is defined as the number 
										of synapses in a "connected state" (connected synapses) 
										that are connected to input bits which are turned on.
		numActive:			The intended number of columns to survive inhibition.
		"""
		#calculate num active per inhibition area
		activeColumns = numpy.zeros(self._numColumns)
		winners = sorted(range(overlaps.size), 
										 key=lambda k: overlaps[k], 
										 reverse=True)[0:numActive]
		activeColumns[winners] = 1
		return numpy.where(activeColumns > 0)[0]


	def _inhibitColumnsLocal(self, overlaps, numActive):
		"""
		Performs local inhibition. Local inhibition is performed on a column by 
		column basis. Each column observes the overlaps of its neighbors and is 
		selected if its overlap score is within the top 'numActive' in its local 
		neighborhood.

		Parameters:
		----------------------------
		overlaps:				an array containing the overlap score for each  column. 
										The overlap score for a column is defined as the number 
										of synapses in a "connected state" (connected synapses) 
										that are connected to input bits which are turned on.
		numActive:			The intended number of columns to survive inhibition. This
										number is only an intended target. Since the surviving
										columns are picked in a local fashion, the exact number 
										of survining columns is likely to vary.
		"""
		activeColumns = numpy.zeros(self._numColumns)
		addToWinners = max(overlaps)/1000.0   
		overlaps = numpy.array(overlaps, dtype=realDType).reshape(
			self._columnDimensions)
		for i in xrange(self._numColumns):
			maskNeighbors = self._getNeighborsND(i, self._columnDimensions,
				self._inhibitionRadius)
			overlapSlice = overlaps[maskNeighbors]
			kthLargestValue = sorted(overlapSlice,
															 reverse=True)[numActive-1]
			if overlaps[i] >= kthLargestValue:
				activeColumns[i] = 1
				overlaps[i] += addToWinners
		return numpy.where(activeColumns > 0)[0]


	@staticmethod
	def _getNeighbors1D(columnIndex, dimensions, radius):
		"""
		Returns a list of indices corresponding to the neighbors of a given column. 
		In this variation of the method, which only supports a one dimensional 
		column toplogy, a column's neighbors are those neighbors who are 'radius'
		indices away. This information is needed to perform inhibition. This method
		is a subset of _getNeighborsND and is only included for illustration 
		purposes, and potentially enhanced performance for spatial pooler 
		implementations that only require a one-dimensional topology.

		Parameters:
		----------------------------
		columnIndex: 		The index identifying a column in the permanence, potential 
										and connectivity matrices.
		dimensions:			An array containg a dimensions for the column space. A 2x3
										grid will be represented by [2,3].
		radius:					Indicates how far away from a given column are other 
										columns to be considered its neighbors. In the previous 2x3
										example, each column with coordinates:
										[2+/-radius, 3+/-radius] is considered a neighbor.

		"""
		assert(dimensions.size == 1)
		ncols = dimensions[0]
		neighbors = numpy.array(
			range(columnIndex-radius,columnIndex+radius+1)) % ncols
		neighbors = list(set(neighbors) - set([columnIndex])) 
		assert(neighbors)
		return neighbors


	@staticmethod
	def _getNeighbors2D(columnIndex, dimensions, radius):
		"""
		Returns a list of indices corresponding to the neighbors of a given column.
		Since the permanence values are stored in such a way that information about 
		toplogy is lost, this method allows for reconstructing the toplogy of the 
		inputs, which are flattened to one array. Given a column's index, its 
		neighbors are defined as those columns that are 'radius' indices away from 
		it in each dimension. The method returns a list of the flat indices of 
		these columns. This method is a subset of _getNeighborsND and is only 
		included for illustration purposes, and potentially enhanced performance 
		for spatial pooler implementations that only require a two-dimensional 
		topology.

		Parameters:
		----------------------------
		columnIndex: 		The index identifying a column in the permanence, potential 
										and connectivity matrices.
		dimensions:			An array containg a dimensions for the column space. A 2x3
										grid will be represented by [2,3].
		radius:					Indicates how far away from a given column are other 
										columns to be considered its neighbors. In the previous 2x3
										example, each column with coordinates:
										[2+/-radius, 3+/-radius] is considered a neighbor.
		"""
		assert(dimensions.size == 2)
		nrows = dimensions[0]
		ncols = dimensions[1]

		toRow = lambda index: index / ncols
		toCol = lambda index: index % ncols
		toIndex = lambda row, col: row * ncols + col

		row = toRow(columnIndex)
		col = toCol(columnIndex)

		# to disable wrap around use clip instead
		colRange = numpy.array(range(row-radius, row+radius+1)) % nrows
		rowRange = numpy.array(range(col-radius, col+radius+1)) % ncols

		neighbors = [toIndex(r, c) for (r, c) in 
			itertools.product(colRange, rowRange)]
		neighbors = list(set(neighbors) - set([columnIndex]))
		assert(neighbors)
		return neighbors
		 

	@staticmethod
	def _getNeighborsND(columnIndex, dimensions, radius):
		"""
		Similar to _getNeighbors1D and _getNeighbors2D, this function Returns a 
		list of indices corresponding to the neighbors of a given column. Since the 
		permanence values are stored in such a way that information about toplogy 
		is lost. This method allows for reconstructing the toplogy of the inputs, 
		which are flattened to one array. Given a column's index, its neighbors are 
		defined as those columns that are 'radius' indices away from it in each 
		dimension. The method returns a list of the flat indices of these columns. 
		Parameters:
		----------------------------
		columnIndex: 		The index identifying a column in the permanence, potential 
										and connectivity matrices.
		dimensions:			An array containg a dimensions for the column space. A 2x3
										grid will be represented by [2,3].
		radius:					Indicates how far away from a given column are other 
										columns to be considered its neighbors. In the previous 2x3
										example, each column with coordinates:
										[2+/-radius, 3+/-radius] is considered a neighbor.
		"""
		assert(dimensions.size > 0)
		bounds = numpy.cumprod(numpy.append([1], dimensions[::-1][:-1]))[::-1]

		def toCoords(index):
			return (index / bounds) % dimensions

		def toIndex(coords):
			return numpy.dot(bounds, coords)

		columnCoords = toCoords(columnIndex)
		rangeND = []
		for i in xrange(dimensions.size):
			curRange = numpy.array(range(columnCoords[i]-radius, 
																	 columnCoords[i]+radius+1)) % dimensions[i]
			# this version disables wrap around
			# curRange = numpy.array(range(columnCoords[i]-radius, \
			#                              columnCoords[i]+radius+1))
			# curRange = numpy.clip(curRange,0,dimensions[i])
			rangeND.append(curRange)

		neighbors = [toIndex(numpy.array(coord)) for coord in 
			itertools.product(*rangeND)]
		neighbors = list(set(neighbors) - set([columnIndex]))
		assert(neighbors)
		return neighbors


	def _isUpdateRound(self):
		"""
		returns true if the enough rounds have passed to warrant updates of 
		duty cycles
		"""
		return ((self._iterationNum + 1) % self._updatePeriod) == 0


	def _seed(self, seed=-1):
		"""
		Initialize the random seed
		"""
		if seed != -1:
			self._random = NupicRandom(seed)
			random.seed(seed)
			numpy.random.seed(seed)
		else:
			self._random = NupicRandom()
		

	def __get_state__(self):
		if not hasattr(self,"_version"):
			pass

	def __set_state__(self):
		pass
