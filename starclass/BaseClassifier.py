#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
The basic stellar classifier class for the TASOC pipeline.
All other specific stellar classification algorithms will inherit from BaseClassifier.

.. codeauthor:: Rasmus Handberg <rasmush@phys.au.dk>
"""

import warnings
warnings.filterwarnings('ignore', category=DeprecationWarning, message='Using or importing the ABCs from \'collections\' instead of from \'collections.abc\' is deprecated')
import numpy as np
import os.path
import logging
from lightkurve import TessLightCurve
from astropy.io import fits
from tqdm import tqdm
from sklearn.metrics import accuracy_score, confusion_matrix
from .StellarClasses import StellarClasses
from .features.freqextr import freqextr
from .features.fliper import FliPer
from .features.powerspectrum import powerspectrum
from .utilities import savePickle, loadPickle
from .plots import plotConfMatrix, plt

__docformat__ = 'restructuredtext'

class BaseClassifier(object):
	"""
	The basic stellar classifier class for the TASOC pipeline.
	All other specific stellar classification algorithms will inherit from BaseClassifier.

	.. codeauthor:: Rasmus Handberg <rasmush@phys.au.dk>
	"""

	def __init__(self, tset_key=None, features_cache=None, level='L1', plot=False):
		"""
		Initialize the classifier object.

		Parameters:
			tset_key (string): From which training-set should the classifier be loaded?
			level (string, optional): Classfication-level to load. Coices are ``'L1'`` and ``'L2'``. Default='L1'.
			features_cache (string, optional): Path to director where calculated features will be saved/loaded as needed.
			plot (boolean, optional): Create plots as part of the output. Default is ``False``.

		Attributes:
			plot (boolean): Indicates wheter plotting is enabled.
			data_dir (string): Path to directory where classifiers store auxiliary data.
				Different directories will be used for each classification level.
		"""

		# Check the input:
		assert level in ('L1', 'L2'), "Invalid level"

		# Start logger:
		logger = logging.getLogger(__name__)

		# Store the input:
		self.tset_key = tset_key
		self.plot = plot
		self.level = level
		self.features_cache = features_cache

		self.data_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), 'data', level, tset_key))
		logger.debug("Data Directory: %s", self.data_dir)
		os.makedirs(self.data_dir, exist_ok=True)

		if self.features_cache is not None and not os.path.exists(self.features_cache):
			raise ValueError("features_cache directory does not exists")

		self.classifier_key = {
			'BaseClassifier': 'base',
			'RFGCClassifier': 'rfgc',
			'SLOSHClassifier': 'slosh',
			'MetaClassifier': 'meta'
		}[self.__class__.__name__]

	def __enter__(self):
		return self

	def __exit__(self, *args):
		self.close()

	def close(self):
		"""Close the classifier."""
		pass

	def classify(self, features):
		"""
		Classify a star from the lightcurve and other features.

		Will run the :py:func:`do_classify` method and
		check some of the output and calculate various
		performance metrics.

		Parameters:
			features (dict): Dictionary of features, including the lightcurve itself.

		Returns:
			dict: Dictionary of classifications

		See Also:
			:py:func:`do_classify`

		.. codeauthor:: Rasmus Handberg <rasmush@phys.au.dk>
		"""
		res = self.do_classify(features)
		# Check results
		for key, value in res.items():
			if key not in StellarClasses:
				raise ValueError("Classifier returned unknown stellar class: '%s'" % key)
			if value < 0 or value > 1:
				raise ValueError("Classifier should return probability between 0 and 1.")

		return res

	def do_classify(self, features):
		"""
		This method should be overwritten by child classes.

		Parameters:
			features (dict): Dictionary of features of star, including the lightcurve itself.

		Returns:
			dict: Dictionary where the keys should be from ``StellarClasses`` and the
			corresponding values indicate the probability of the star belonging to
			that class.

		Raises:
			NotImplementedError
		"""
		raise NotImplementedError()

	def train(self, tset):
		"""
		Parameters:
			tset (``TrainingSet`` object): Training-set to train classifier on.

		Raises:
			NotImplementedError
		"""
		raise NotImplementedError()

	def test(self, tset, save=False, save_func=None):
		"""
		Parameters:
			tset (``TrainingSet`` object): Training-set to run testing on.
		"""

		if tset.testfraction == 0:
			return

		# Start logger:
		logger = logging.getLogger(__name__)

		# TODO: Only include classes from the current level
		all_classes = [lbl.value for lbl in StellarClasses]

		# Classify test set (has to be one by one unless we change classifiers)
		# TODO: Use TaskManager for this?
		y_pred = []
		for features in tqdm(tset.features_test(), total=len(tset.test_idx)):
			# Classify this star from the test-set:
			res = self.classify(features)

			prediction = max(res, key=lambda key: res[key]).value
			y_pred.append(prediction)

			# TODO: Save results for this classifier/trainingset in database
			if save:
				res.update({
					'priority': features['priority'],
					'classifier': self.classifier_key,
					'status': 1
				})
				logger.debug(res)
				save_func(res)

		y_pred = np.array(y_pred)

		# Convert labels to ndarray:
		# FIXME: Only keeping the first label
		labels_test = np.array([lbl[0].value for lbl in tset.labels_test(level=self.level)])

		# Compare to known labels:
		acc = accuracy_score(labels_test, y_pred)
		logger.info('Accuracy: %.2f%%', acc*100)

		# Confusion Matrix:
		cf = confusion_matrix(labels_test, y_pred, labels=all_classes)

		fig = plt.figure(figsize=(12,12))
		plotConfMatrix(cf, all_classes)
		plt.title(self.classifier_key + ' - ' + tset.key + ' - ' + self.level)
		fig.savefig(os.path.join(self.data_dir, 'confusion_matrix_'  + tset.key + '_' + self.level + '_' + self.classifier_key + '.png'), bbox_inches='tight')
		plt.close(fig)


	def load_star(self, task, fname):
		"""Recieve a task from the TaskManager and load the lightcurve."""

		logger = logging.getLogger(__name__)

		# Load lightcurve file and create a TessLightCurve object:
		if fname.endswith('.noisy') or fname.endswith('.sysnoise') or fname.endswith('.txt'):
			data = np.loadtxt(fname)
			if data.shape[1] == 4:
				quality = np.asarray(data[:,3], dtype='int32')
			else:
				quality = np.zeros(data.shape[0], dtype='int32')

			lightcurve = TessLightCurve(
				time=data[:,0],
				flux=data[:,1],
				flux_err=data[:,2],
				quality=quality,
				time_format='jd',
				time_scale='tdb',
				targetid=task['starid'],
				camera=task['camera'],
				ccd=task['ccd'],
				sector=2,
				#ra=0,
				#dec=0,
				quality_bitmask=2+8+256, # lightkurve.utils.TessQualityFlags.DEFAULT_BITMASK,
				meta={}
			)

		elif fname.endswith('.fits') or fname.endswith('.fits.gz'):
			with fits.open(fname, mode='readonly', memmap=True) as hdu:
				lightcurve = TessLightCurve(
					time=hdu['LIGHTCURVE'].data['TIME'],
					flux=hdu['LIGHTCURVE'].data['FLUX_CORR'],
					flux_err=hdu['LIGHTCURVE'].data['FLUX_CORR_ERR'],
					centroid_col=hdu['LIGHTCURVE'].data['MOM_CENTR1'],
					centroid_row=hdu['LIGHTCURVE'].data['MOM_CENTR2'],
					quality=np.asarray(hdu['LIGHTCURVE'].data['QUALITY'], dtype='int32'),
					cadenceno=np.asarray(hdu['LIGHTCURVE'].data['CADENCENO'], dtype='int32'),
					time_format='btjd',
					time_scale='tdb',
					targetid=hdu[0].header.get('TICID'),
					label=hdu[0].header.get('OBJECT'),
					camera=hdu[0].header.get('CAMERA'),
					ccd=hdu[0].header.get('CCD'),
					sector=hdu[0].header.get('SECTOR'),
					ra=hdu[0].header.get('RA_OBJ'),
					dec=hdu[0].header.get('DEC_OBJ'),
					quality_bitmask=2+8+256, # lightkurve.utils.TessQualityFlags.DEFAULT_BITMASK
					meta={}
				)

		else:
			raise ValueError("Invalid file format")

		# Load features from cache file, or calculate them
		# and put them into cache file for other classifiers
		# to use later on:
		features = None
		loaded_from_cache = False
		if self.features_cache:
			features_file = os.path.join(self.features_cache, 'features-' + str(task['priority']) + '.pickle')
			if os.path.exists(features_file):
				loaded_from_cache = True
				features = loadPickle(features_file)

		# No features found in cache, so calculate them:
		if features is None:
			features = self.calc_features(lightcurve)
			logger.debug(features)

		# Add the fields from the task to the list of features:
		features['priority'] = task['priority']
		features['starid'] = task['starid']
		for key in ('tmag', 'mean_flux', 'variance', 'variability', 'other_classifiers'):
			if key in task.keys():
				features[key] = task[key]

		# Save features in cache file for later use:
		if self.features_cache and not loaded_from_cache:
			savePickle(features_file, features)

		return features

	def calc_features(self, lightcurve):
		"""Calculate other derived features from the lightcurve."""

		# We start out with an empty list of features:
		features = {}

		# Add the lightcurve as a seperate feature:
		features['lightcurve'] = lightcurve

		# Prepare lightcurve for power spectrum calculation:
		# NOTE: Lightcurves are now in relative flux (ppm) with zero mean!
		lc = lightcurve.remove_nans()
		#lc = lc.remove_outliers(5.0, stdfunc=mad_std) # Sigma clipping

		# Calculate power spectrum:
		psd = powerspectrum(lc)

		# Save the entire power spectrum object in the features:
		features['powerspectrum'] = psd

		# Extract primary frequencies from lightcurve and add to features:
		features.update(freqextr(lightcurve))

		# Calculate FliPer features:
		features.update(FliPer(psd))

		return features