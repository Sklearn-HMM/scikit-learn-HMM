# Authors: Peter Prettenhofer <peter.prettenhofer@gmail.com> (main author)
#          Mathieu Blondel (partial_fit support)
#
# License: BSD 3 clause
"""Classification and regression using Stochastic Gradient Descent (SGD)."""

import numpy as np
import scipy.sparse as sp

from abc import ABCMeta, abstractmethod
import warnings

from ..externals.joblib import Parallel, delayed

from .base import LinearClassifierMixin, SparseCoefMixin
from ..base import BaseEstimator, RegressorMixin
from ..feature_selection.from_model import _LearntSelectorMixin
from ..utils import (array2d, atleast2d_or_csr, check_arrays, deprecated,
                     column_or_1d)
from ..utils.extmath import safe_sparse_dot
from ..utils.multiclass import _check_partial_fit_first_call
from ..externals import six

from .sgd_fast import plain_sgd as plain_sgd
from ..utils.seq_dataset import ArrayDataset, CSRDataset
from ..utils import compute_class_weight
from .sgd_fast import Hinge
from .sgd_fast import SquaredHinge
from .sgd_fast import Log
from .sgd_fast import ModifiedHuber
from .sgd_fast import SquaredLoss
from .sgd_fast import Huber
from .sgd_fast import EpsilonInsensitive
from .sgd_fast import SquaredEpsilonInsensitive


LEARNING_RATE_TYPES = {"constant": 1, "optimal": 2, "invscaling": 3,
                       "pa1": 4, "pa2": 5}

PENALTY_TYPES = {"none": 0, "l2": 2, "l1": 1, "elasticnet": 3}

SPARSE_INTERCEPT_DECAY = 0.01
"""For sparse data intercept updates are scaled by this decay factor to avoid
intercept oscillation."""

DEFAULT_EPSILON = 0.1
"""Default value of ``epsilon`` parameter. """


class BaseSGD(six.with_metaclass(ABCMeta, BaseEstimator, SparseCoefMixin)):
    """Base class for SGD classification and regression."""

    def __init__(self, loss, penalty='l2', alpha=0.0001, C=1.0,
                 l1_ratio=0.15, fit_intercept=True, n_iter=5, shuffle=False,
                 verbose=0, epsilon=0.1, random_state=None,
                 learning_rate="optimal", eta0=0.0, power_t=0.5,
                 warm_start=False):
        self.loss = loss
        self.penalty = penalty
        self.learning_rate = learning_rate
        self.epsilon = epsilon
        self.alpha = alpha
        self.C = C
        self.l1_ratio = l1_ratio
        self.fit_intercept = fit_intercept
        self.n_iter = n_iter
        self.shuffle = shuffle
        self.random_state = random_state
        self.verbose = verbose
        self.eta0 = eta0
        self.power_t = power_t
        self.warm_start = warm_start

        self._validate_params()

        self.coef_ = None
        # iteration count for learning rate schedule
        # must not be int (e.g. if ``learning_rate=='optimal'``)
        self.t_ = None

    def set_params(self, *args, **kwargs):
        super(BaseSGD, self).set_params(*args, **kwargs)
        self._validate_params()
        return self

    @abstractmethod
    def fit(self, X, y):
        """Fit model."""

    def _validate_params(self):
        """Validate input params. """
        if not isinstance(self.shuffle, bool):
            raise ValueError("shuffle must be either True or False")
        if self.n_iter <= 0:
            raise ValueError("n_iter must be > zero")
        if not (0.0 <= self.l1_ratio <= 1.0):
            raise ValueError("l1_ratio must be in [0, 1]")
        if self.alpha < 0.0:
            raise ValueError("alpha must be >= 0")
        if self.learning_rate in ("constant", "invscaling"):
            if self.eta0 <= 0.0:
                raise ValueError("eta0 must be > 0")

        # raises ValueError if not registered
        self._get_penalty_type(self.penalty)
        self._get_learning_rate_type(self.learning_rate)

        if self.loss not in self.loss_functions:
            raise ValueError("The loss %s is not supported. " % self.loss)

    def _init_t(self, loss_function):
        """Initialize iteration counter attr ``t_``.

        If ``self.learning_rate=='optimal'`` initialize ``t_`` such that
        ``eta`` at first sample equals ``self.eta0``.
        """
        self.t_ = 1.0
        if self.learning_rate == "optimal":
            typw = np.sqrt(1.0 / np.sqrt(self.alpha))
            # computing eta0, the initial learning rate
            eta0 = typw / max(1.0, loss_function.dloss(-typw, 1.0))
            # initialize t such that eta at first sample equals eta0
            self.t_ = 1.0 / (eta0 * self.alpha)

    def _get_loss_function(self, loss):
        """Get concrete ``LossFunction`` object for str ``loss``. """
        try:
            loss_ = self.loss_functions[loss]
            loss_class, args = loss_[0], loss_[1:]
            if loss in ('huber', 'epsilon_insensitive',
                        'squared_epsilon_insensitive'):
                args = (self.epsilon, )
            return loss_class(*args)
        except KeyError:
            raise ValueError("The loss %s is not supported. " % loss)

    def _get_learning_rate_type(self, learning_rate):
        try:
            return LEARNING_RATE_TYPES[learning_rate]
        except KeyError:
            raise ValueError("learning rate %s "
                             "is not supported. " % learning_rate)

    def _get_penalty_type(self, penalty):
        penalty = str(penalty).lower()
        try:
            return PENALTY_TYPES[penalty]
        except KeyError:
            raise ValueError("Penalty %s is not supported. " % penalty)

    def _validate_sample_weight(self, sample_weight, n_samples):
        """Set the sample weight array."""
        if sample_weight is None:
            # uniform sample weights
            sample_weight = np.ones(n_samples, dtype=np.float64, order='C')
        else:
            # user-provided array
            sample_weight = np.asarray(sample_weight, dtype=np.float64,
                                       order="C")
        if sample_weight.shape[0] != n_samples:
            raise ValueError("Shapes of X and sample_weight do not match.")
        return sample_weight

    def _allocate_parameter_mem(self, n_classes, n_features, coef_init=None,
                                intercept_init=None):
        """Allocate mem for parameters; initialize if provided."""
        if n_classes > 2:
            # allocate coef_ for multi-class
            if coef_init is not None:
                coef_init = np.asarray(coef_init, order="C")
                if coef_init.shape != (n_classes, n_features):
                    raise ValueError("Provided coef_ does not match dataset. ")
                self.coef_ = coef_init
            else:
                self.coef_ = np.zeros((n_classes, n_features),
                                      dtype=np.float64, order="C")

            # allocate intercept_ for multi-class
            if intercept_init is not None:
                intercept_init = np.asarray(intercept_init, order="C")
                if intercept_init.shape != (n_classes, ):
                    raise ValueError("Provided intercept_init "
                                     "does not match dataset.")
                self.intercept_ = intercept_init
            else:
                self.intercept_ = np.zeros(n_classes, dtype=np.float64,
                                           order="C")
        else:
            # allocate coef_ for binary problem
            if coef_init is not None:
                coef_init = np.asarray(coef_init, dtype=np.float64,
                                       order="C")
                coef_init = coef_init.ravel()
                if coef_init.shape != (n_features,):
                    raise ValueError("Provided coef_init does not "
                                     "match dataset.")
                self.coef_ = coef_init
            else:
                self.coef_ = np.zeros(n_features, dtype=np.float64, order="C")

            # allocate intercept_ for binary problem
            if intercept_init is not None:
                intercept_init = np.asarray(intercept_init, dtype=np.float64)
                if intercept_init.shape != (1,) and intercept_init.shape != ():
                    raise ValueError("Provided intercept_init "
                                     "does not match dataset.")
                self.intercept_ = intercept_init.reshape(1,)
            else:
                self.intercept_ = np.zeros(1, dtype=np.float64, order="C")


def _check_fit_data(X, y):
    """Check if shape of input data matches. """
    n_samples, _ = X.shape
    if n_samples != y.shape[0]:
        raise ValueError("Shapes of X and y do not match.")


def _make_dataset(X, y_i, sample_weight):
    """Create ``Dataset`` abstraction for sparse and dense inputs.

    This also returns the ``intercept_decay`` which is different
    for sparse datasets.
    """
    if sp.issparse(X):
        dataset = CSRDataset(X.data, X.indptr, X.indices, y_i, sample_weight)
        intercept_decay = SPARSE_INTERCEPT_DECAY
    else:
        dataset = ArrayDataset(X, y_i, sample_weight)
        intercept_decay = 1.0
    return dataset, intercept_decay


def _prepare_fit_binary(est, y, i):
    """Initialization for fit_binary.

    Returns y, coef, intercept.
    """
    y_i = np.ones(y.shape, dtype=np.float64, order="C")
    y_i[y != est.classes_[i]] = -1.0

    if len(est.classes_) == 2:
        coef = est.coef_.ravel()
        intercept = est.intercept_[0]
    else:
        coef = est.coef_[i]
        intercept = est.intercept_[i]

    return y_i, coef, intercept


def fit_binary(est, i, X, y, alpha, C, learning_rate, n_iter,
               pos_weight, neg_weight, sample_weight):
    """Fit a single binary classifier.

    The i'th class is considered the "positive" class.
    """
    y_i, coef, intercept = _prepare_fit_binary(est, y, i)
    assert y_i.shape[0] == y.shape[0] == sample_weight.shape[0]
    dataset, intercept_decay = _make_dataset(X, y_i, sample_weight)

    penalty_type = est._get_penalty_type(est.penalty)
    learning_rate_type = est._get_learning_rate_type(learning_rate)

    return plain_sgd(coef, intercept, est.loss_function,
                     penalty_type, alpha, C, est.l1_ratio,
                     dataset, n_iter, int(est.fit_intercept),
                     int(est.verbose), int(est.shuffle), est.random_state,
                     pos_weight, neg_weight,
                     learning_rate_type, est.eta0,
                     est.power_t, est.t_, intercept_decay)


class BaseSGDClassifier(six.with_metaclass(ABCMeta, BaseSGD,
                                           LinearClassifierMixin)):

    loss_functions = {
        "hinge": (Hinge, 1.0),
        "squared_hinge": (SquaredHinge, 1.0),
        "perceptron": (Hinge, 0.0),
        "log": (Log, ),
        "modified_huber": (ModifiedHuber, ),
        "squared_loss": (SquaredLoss, ),
        "huber": (Huber, DEFAULT_EPSILON),
        "epsilon_insensitive": (EpsilonInsensitive, DEFAULT_EPSILON),
        "squared_epsilon_insensitive": (SquaredEpsilonInsensitive,
                                        DEFAULT_EPSILON),
    }

    @abstractmethod
    def __init__(self, loss="hinge", penalty='l2', alpha=0.0001, l1_ratio=0.15,
                 fit_intercept=True, n_iter=5, shuffle=False, verbose=0,
                 epsilon=DEFAULT_EPSILON, n_jobs=1, random_state=None,
                 learning_rate="optimal", eta0=0.0, power_t=0.5,
                 class_weight=None, warm_start=False, seed=None):

        if seed is not None:
            warnings.warn("Parameter 'seed' was renamed to 'random_state' for"
                          " consistency and will be removed in 0.15",
                          DeprecationWarning)
            random_state = seed

        super(BaseSGDClassifier, self).__init__(loss=loss, penalty=penalty,
                                                alpha=alpha, l1_ratio=l1_ratio,
                                                fit_intercept=fit_intercept,
                                                n_iter=n_iter, shuffle=shuffle,
                                                verbose=verbose,
                                                epsilon=epsilon,
                                                random_state=random_state,
                                                learning_rate=learning_rate,
                                                eta0=eta0, power_t=power_t,
                                                warm_start=warm_start)
        self.class_weight = class_weight
        self.classes_ = None
        self.n_jobs = int(n_jobs)

    @property
    @deprecated("Parameter 'seed' was renamed to 'random_state' for"
                " consistency and will be removed in 0.15")
    def seed(self):
        return self.random_state

    def _partial_fit(self, X, y, alpha, C,
                     loss, learning_rate, n_iter,
                     classes, sample_weight,
                     coef_init, intercept_init):
        X = atleast2d_or_csr(X, dtype=np.float64, order="C")
        y = column_or_1d(y, warn=True)

        n_samples, n_features = X.shape
        _check_fit_data(X, y)

        self._validate_params()
        _check_partial_fit_first_call(self, classes)

        n_classes = self.classes_.shape[0]

        # Allocate datastructures from input arguments
        y_ind = np.searchsorted(self.classes_, y)   # XXX use a LabelBinarizer?
        self._expanded_class_weight = compute_class_weight(self.class_weight,
                                                           self.classes_,
                                                           y_ind)
        sample_weight = self._validate_sample_weight(sample_weight, n_samples)

        if self.coef_ is None or coef_init is not None:
            self._allocate_parameter_mem(n_classes, n_features,
                                         coef_init, intercept_init)

        self.loss_function = self._get_loss_function(loss)
        if self.t_ is None:
            self._init_t(self.loss_function)

        # delegate to concrete training procedure
        if n_classes > 2:
            self._fit_multiclass(X, y, alpha=alpha, C=C,
                                 learning_rate=learning_rate,
                                 sample_weight=sample_weight, n_iter=n_iter)
        elif n_classes == 2:
            self._fit_binary(X, y, alpha=alpha, C=C,
                             learning_rate=learning_rate,
                             sample_weight=sample_weight, n_iter=n_iter)
        else:
            raise ValueError("The number of class labels must be "
                             "greater than one.")

        self.t_ += n_iter * n_samples

        return self

    def _fit(self, X, y, alpha, C, loss, learning_rate,
             coef_init=None, intercept_init=None, class_weight=None,
             sample_weight=None):
        if hasattr(self, "classes_"):
            self.classes_ = None

        if class_weight is not None:
            warnings.warn("Using 'class_weight' as a parameter to the 'fit'"
                          " method is deprecated and will be removed in 0.13. "
                          "Set it on initialization instead.",
                          DeprecationWarning, stacklevel=2)

            self.class_weight = class_weight

        X = atleast2d_or_csr(X, dtype=np.float64, order="C")
        n_samples, n_features = X.shape

        # labels can be encoded as float, int, or string literals
        # np.unique sorts in asc order; largest class id is positive class
        classes = np.unique(y)

        if self.warm_start and self.coef_ is not None:
            if coef_init is None:
                coef_init = self.coef_
            if intercept_init is None:
                intercept_init = self.intercept_
        else:
            self.coef_ = None
            self.intercept_ = None

        # Clear iteration count for multiple call to fit.
        self.t_ = None

        self._partial_fit(X, y, alpha, C, loss, learning_rate, self.n_iter,
                          classes, sample_weight, coef_init, intercept_init)

        return self

    def _fit_binary(self, X, y, alpha, C, sample_weight,
                    learning_rate, n_iter):
        """Fit a binary classifier on X and y. """
        coef, intercept = fit_binary(self, 1, X, y, alpha, C,
                                     learning_rate, n_iter,
                                     self._expanded_class_weight[1],
                                     self._expanded_class_weight[0],
                                     sample_weight)
        # need to be 2d
        self.coef_ = coef.reshape(1, -1)
        # intercept is a float, need to convert it to an array of length 1
        self.intercept_ = np.atleast_1d(intercept)

    def _fit_multiclass(self, X, y, alpha, C, learning_rate,
                        sample_weight, n_iter):
        """Fit a multi-class classifier by combining binary classifiers

        Each binary classifier predicts one class versus all others. This
        strategy is called OVA: One Versus All.
        """
        # Use joblib to fit OvA in parallel
        result = Parallel(n_jobs=self.n_jobs, verbose=self.verbose)(
            delayed(fit_binary)(self, i, X, y, alpha, C, learning_rate,
                                n_iter, self._expanded_class_weight[i], 1.,
                                sample_weight)
            for i in range(len(self.classes_)))

        for i, (coef, intercept) in enumerate(result):
            self.coef_[i] = coef
            self.intercept_[i] = intercept

    def partial_fit(self, X, y, classes=None, sample_weight=None):
        """Fit linear model with Stochastic Gradient Descent.

        Parameters
        ----------
        X : {array-like, sparse matrix}, shape = [n_samples, n_features]
            Subset of the training data

        y : numpy array of shape [n_samples]
            Subset of the target values

        classes : array, shape = [n_classes]
            Classes across all calls to partial_fit.
            Can be obtained by via `np.unique(y_all)`, where y_all is the
            target vector of the entire dataset.
            This argument is required for the first call to partial_fit
            and can be omitted in the subsequent calls.
            Note that y doesn't need to contain all labels in `classes`.

        sample_weight : array-like, shape = [n_samples], optional
            Weights applied to individual samples.
            If not provided, uniform weights are assumed.

        Returns
        -------
        self : returns an instance of self.
        """
        return self._partial_fit(X, y, alpha=self.alpha, C=1.0, loss=self.loss,
                                 learning_rate=self.learning_rate, n_iter=1,
                                 classes=classes, sample_weight=sample_weight,
                                 coef_init=None, intercept_init=None)

    def fit(self, X, y, coef_init=None, intercept_init=None,
            class_weight=None, sample_weight=None):
        """Fit linear model with Stochastic Gradient Descent.

        Parameters
        ----------
        X : {array-like, sparse matrix}, shape = [n_samples, n_features]
            Training data

        y : numpy array of shape [n_samples]
            Target values

        coef_init : array, shape = [n_classes,n_features]
            The initial coefficients to warm-start the optimization.

        intercept_init : array, shape = [n_classes]
            The initial intercept to warm-start the optimization.

        sample_weight : array-like, shape = [n_samples], optional
            Weights applied to individual samples.
            If not provided, uniform weights are assumed.

        Returns
        -------
        self : returns an instance of self.
        """
        return self._fit(X, y, alpha=self.alpha, C=1.0,
                         loss=self.loss, learning_rate=self.learning_rate,
                         coef_init=coef_init, intercept_init=intercept_init,
                         class_weight=class_weight,
                         sample_weight=sample_weight)


class SGDClassifier(BaseSGDClassifier, _LearntSelectorMixin):
    """Linear classifiers (SVM, logistic regression, a.o.) with SGD training.

    This estimator implements regularized linear models with stochastic
    gradient descent (SGD) learning: the gradient of the loss is estimated
    each sample at a time and the model is updated along the way with a
    decreasing strength schedule (aka learning rate). SGD allows minibatch
    (online/out-of-core) learning, see the partial_fit method.

    This implementation works with data represented as dense or sparse arrays
    of floating point values for the features. The model it fits can be
    controlled with the loss parameter; by default, it fits a linear support
    vector machine (SVM).

    The regularizer is a penalty added to the loss function that shrinks model
    parameters towards the zero vector using either the squared euclidean norm
    L2 or the absolute norm L1 or a combination of both (Elastic Net). If the
    parameter update crosses the 0.0 value because of the regularizer, the
    update is truncated to 0.0 to allow for learning sparse models and achieve
    online feature selection.

    Parameters
    ----------
    loss : str, 'hinge', 'log', 'modified_huber', 'squared_hinge',\
                'perceptron', or a regression loss: 'squared_loss', 'huber',\
                'epsilon_insensitive', or 'squared_epsilon_insensitive'
        The loss function to be used. Defaults to 'hinge', which gives a
        linear SVM.
        The 'log' loss gives logistic regression, a probabilistic classifier.
        'modified_huber' is another smooth loss that brings tolerance to
        outliers as well as probability estimates.
        'squared_hinge' is like hinge but is quadratically penalized.
        'perceptron' is the linear loss used by the perceptron algorithm.
        The other losses are designed for regression but can be useful in
        classification as well; see SGDRegressor for a description.

    penalty : str, 'l2' or 'l1' or 'elasticnet'
        The penalty (aka regularization term) to be used. Defaults to 'l2'
        which is the standard regularizer for linear SVM models. 'l1' and
        'elasticnet' migh bring sparsity to the model (feature selection)
        not achievable with 'l2'.

    alpha : float
        Constant that multiplies the regularization term. Defaults to 0.0001

    l1_ratio : float
        The Elastic Net mixing parameter, with 0 <= l1_ratio <= 1.
        l1_ratio=0 corresponds to L2 penalty, l1_ratio=1 to L1.
        Defaults to 0.15.

    fit_intercept: bool
        Whether the intercept should be estimated or not. If False, the
        data is assumed to be already centered. Defaults to True.

    n_iter: int, optional
        The number of passes over the training data (aka epochs).
        Defaults to 5.

    shuffle: bool, optional
        Whether or not the training data should be shuffled after each epoch.
        Defaults to False.

    random_state: int seed, RandomState instance, or None (default)
        The seed of the pseudo random number generator to use when
        shuffling the data.

    verbose: integer, optional
        The verbosity level

    epsilon: float
        Epsilon in the epsilon-insensitive loss functions; only if `loss` is
        'huber', 'epsilon_insensitive', or 'squared_epsilon_insensitive'.
        For 'huber', determines the threshold at which it becomes less
        important to get the prediction exactly right.
        For epsilon-insensitive, any differences between the current prediction
        and the correct label are ignored if they are less than this threshold.

    n_jobs: integer, optional
        The number of CPUs to use to do the OVA (One Versus All, for
        multi-class problems) computation. -1 means 'all CPUs'. Defaults
        to 1.

    learning_rate : string, optional
        The learning rate:
        constant: eta = eta0
        optimal: eta = 1.0 / (t + t0) [default]
        invscaling: eta = eta0 / pow(t, power_t)

    eta0 : double
        The initial learning rate for the 'constant' or 'invscaling'
        schedules. The default value is 0.0 as eta0 is not used by the
        default schedule 'optimal'.

    power_t : double
        The exponent for inverse scaling learning rate [default 0.5].

    class_weight : dict, {class_label : weight} or "auto" or None, optional
        Preset for the class_weight fit parameter.

        Weights associated with classes. If not given, all classes
        are supposed to have weight one.

        The "auto" mode uses the values of y to automatically adjust
        weights inversely proportional to class frequencies.

    warm_start : bool, optional
        When set to True, reuse the solution of the previous call to fit as
        initialization, otherwise, just erase the previous solution.

    Attributes
    ----------
    `coef_` : array, shape = [1, n_features] if n_classes == 2 else [n_classes,
    n_features]
        Weights assigned to the features.

    `intercept_` : array, shape = [1] if n_classes == 2 else [n_classes]
        Constants in decision function.

    Examples
    --------
    >>> import numpy as np
    >>> from sklearn import linear_model
    >>> X = np.array([[-1, -1], [-2, -1], [1, 1], [2, 1]])
    >>> Y = np.array([1, 1, 2, 2])
    >>> clf = linear_model.SGDClassifier()
    >>> clf.fit(X, Y)
    ... #doctest: +NORMALIZE_WHITESPACE
    SGDClassifier(alpha=0.0001, class_weight=None, epsilon=0.1, eta0=0.0,
            fit_intercept=True, l1_ratio=0.15, learning_rate='optimal',
            loss='hinge', n_iter=5, n_jobs=1, penalty='l2', power_t=0.5,
            random_state=None, shuffle=False,
            verbose=0, warm_start=False)
    >>> print(clf.predict([[-0.8, -1]]))
    [1]

    See also
    --------
    LinearSVC, LogisticRegression, Perceptron

    """

    def __init__(self, loss="hinge", penalty='l2', alpha=0.0001, l1_ratio=0.15,
                 fit_intercept=True, n_iter=5, shuffle=False, verbose=0,
                 epsilon=DEFAULT_EPSILON, n_jobs=1, random_state=None,
                 learning_rate="optimal", eta0=0.0, power_t=0.5,
                 class_weight=None, warm_start=False, seed=None):
        super(SGDClassifier, self).__init__(
            loss=loss, penalty=penalty, alpha=alpha, l1_ratio=l1_ratio,
            fit_intercept=fit_intercept, n_iter=n_iter, shuffle=shuffle,
            verbose=verbose, epsilon=epsilon, n_jobs=n_jobs,
            random_state=random_state, learning_rate=learning_rate, eta0=eta0,
            power_t=power_t, class_weight=class_weight, warm_start=warm_start,
            seed=seed)

    def predict_proba(self, X):
        """Probability estimates.

        Multiclass probability estimates are derived from binary (one-vs.-rest)
        estimates by simple normalization, as recommended by Zadrozny and
        Elkan.

        Binary probability estimates for loss="modified_huber" are given by
        (clip(decision_function(X), -1, 1) + 1) / 2.

        Parameters
        ----------
        X : {array-like, sparse matrix}, shape = [n_samples, n_features]

        Returns
        -------
        array, shape = [n_samples, n_classes]
            Returns the probability of the sample for each class in the model,
            where classes are ordered as they are in `self.classes_`.

        References
        ----------
        Zadrozny and Elkan, "Transforming classifier scores into multiclass
        probability estimates", SIGKDD'02,
        http://www.research.ibm.com/people/z/zadrozny/kdd2002-Transf.pdf

        The justification for the formula in the loss="modified_huber"
        case is in the appendix B in:
        http://jmlr.csail.mit.edu/papers/volume2/zhang02c/zhang02c.pdf
        """
        if self.loss == "log":
            return self._predict_proba_lr(X)

        elif self.loss == "modified_huber":
            binary = (len(self.classes_) == 2)
            scores = self.decision_function(X)

            if binary:
                prob2 = np.ones((scores.shape[0], 2))
                prob = prob2[:, 1]
            else:
                prob = scores

            np.clip(scores, -1, 1, prob)
            prob += 1.
            prob /= 2.

            if binary:
                prob2[:, 0] -= prob
                prob = prob2
            else:
                # the above might assign zero to all classes, which doesn't
                # normalize neatly; work around this to produce uniform
                # probabilities
                prob_sum = prob.sum(axis=1)
                all_zero = (prob_sum == 0)
                if np.any(all_zero):
                    prob[all_zero, :] = 1
                    prob_sum[all_zero] = len(self.classes_)

                # normalize
                prob /= prob_sum.reshape((prob.shape[0], -1))

            return prob

        else:
            raise NotImplementedError("predict_(log_)proba only supported when"
                                      " loss='log' or loss='modified_huber' "
                                      "(%r given)" % self.loss)

    def predict_log_proba(self, X):
        """Log of probability estimates.

        When loss="modified_huber", probability estimates may be hard zeros
        and ones, so taking the logarithm is not possible.

        Parameters
        ----------
        X : array-like, shape = [n_samples, n_features]

        Returns
        -------
        T : array-like, shape = [n_samples, n_classes]
            Returns the log-probability of the sample for each class in the
            model, where classes are ordered as they are in
            `self.classes_`.
        """
        return np.log(self.predict_proba(X))


class BaseSGDRegressor(BaseSGD, RegressorMixin):

    loss_functions = {
        "squared_loss": (SquaredLoss, ),
        "huber": (Huber, DEFAULT_EPSILON),
        "epsilon_insensitive": (EpsilonInsensitive, DEFAULT_EPSILON),
        "squared_epsilon_insensitive": (SquaredEpsilonInsensitive,
                                        DEFAULT_EPSILON),
    }

    @abstractmethod
    def __init__(self, loss="squared_loss", penalty="l2", alpha=0.0001,
                 l1_ratio=0.15, fit_intercept=True, n_iter=5, shuffle=False,
                 verbose=0, epsilon=DEFAULT_EPSILON, random_state=None,
                 learning_rate="invscaling", eta0=0.01, power_t=0.25,
                 warm_start=False):
        super(BaseSGDRegressor, self).__init__(loss=loss, penalty=penalty,
                                               alpha=alpha, l1_ratio=l1_ratio,
                                               fit_intercept=fit_intercept,
                                               n_iter=n_iter, shuffle=shuffle,
                                               verbose=verbose,
                                               epsilon=epsilon,
                                               random_state=random_state,
                                               learning_rate=learning_rate,
                                               eta0=eta0, power_t=power_t,
                                               warm_start=warm_start)

    def _partial_fit(self, X, y, alpha, C, loss, learning_rate,
                     n_iter, sample_weight,
                     coef_init, intercept_init):
        X, y = check_arrays(X, y, sparse_format="csr", copy=False,
                            check_ccontiguous=True, dtype=np.float64)
        y = column_or_1d(y, warn=True)

        n_samples, n_features = X.shape
        _check_fit_data(X, y)

        self._validate_params()

        # Allocate datastructures from input arguments
        sample_weight = self._validate_sample_weight(sample_weight, n_samples)

        if self.coef_ is None:
            self._allocate_parameter_mem(1, n_features,
                                         coef_init, intercept_init)

        self._fit_regressor(X, y, alpha, C, loss, learning_rate,
                            sample_weight, n_iter)

        self.t_ += n_iter * n_samples

        return self

    def partial_fit(self, X, y, sample_weight=None):
        """Fit linear model with Stochastic Gradient Descent.

        Parameters
        ----------
        X : {array-like, sparse matrix}, shape = [n_samples, n_features]
            Subset of training data

        y : numpy array of shape [n_samples]
            Subset of target values

        sample_weight : array-like, shape = [n_samples], optional
            Weights applied to individual samples.
            If not provided, uniform weights are assumed.

        Returns
        -------
        self : returns an instance of self.
        """
        return self._partial_fit(X, y, self.alpha, C=1.0,
                                 loss=self.loss,
                                 learning_rate=self.learning_rate, n_iter=1,
                                 sample_weight=sample_weight,
                                 coef_init=None, intercept_init=None)

    def _fit(self, X, y, alpha, C, loss, learning_rate, coef_init=None,
             intercept_init=None, sample_weight=None):
        if self.warm_start and self.coef_ is not None:
            if coef_init is None:
                coef_init = self.coef_
            if intercept_init is None:
                intercept_init = self.intercept_
        else:
            self.coef_ = None
            self.intercept_ = None

        # Clear iteration count for multiple call to fit.
        self.t_ = None

        return self._partial_fit(X, y, alpha, C, loss, learning_rate,
                                 self.n_iter, sample_weight,
                                 coef_init, intercept_init)

    def fit(self, X, y, coef_init=None, intercept_init=None,
            sample_weight=None):
        """Fit linear model with Stochastic Gradient Descent.

        Parameters
        ----------
        X : {array-like, sparse matrix}, shape = [n_samples, n_features]
            Training data

        y : numpy array of shape [n_samples]
            Target values

        coef_init : array, shape = [n_features]
            The initial coefficients to warm-start the optimization.

        intercept_init : array, shape = [1]
            The initial intercept to warm-start the optimization.

        sample_weight : array-like, shape = [n_samples], optional
            Weights applied to individual samples (1. for unweighted).

        Returns
        -------
        self : returns an instance of self.
        """
        return self._fit(X, y, alpha=self.alpha, C=1.0,
                         loss=self.loss, learning_rate=self.learning_rate,
                         coef_init=coef_init,
                         intercept_init=intercept_init,
                         sample_weight=sample_weight)

    def decision_function(self, X):
        """Predict using the linear model

        Parameters
        ----------
        X : {array-like, sparse matrix}, shape = [n_samples, n_features]

        Returns
        -------
        array, shape = [n_samples]
           Predicted target values per element in X.
        """
        X = atleast2d_or_csr(X)
        scores = safe_sparse_dot(X, self.coef_.T,
                                 dense_output=True) + self.intercept_
        return scores.ravel()

    def predict(self, X):
        """Predict using the linear model

        Parameters
        ----------
        X : {array-like, sparse matrix}, shape = [n_samples, n_features]

        Returns
        -------
        array, shape = [n_samples]
           Predicted target values per element in X.
        """
        return self.decision_function(X)

    def _fit_regressor(self, X, y, alpha, C, loss, learning_rate,
                       sample_weight, n_iter):
        dataset, intercept_decay = _make_dataset(X, y, sample_weight)

        loss_function = self._get_loss_function(loss)
        penalty_type = self._get_penalty_type(self.penalty)
        learning_rate_type = self._get_learning_rate_type(learning_rate)

        if self.t_ is None:
            self._init_t(loss_function)

        self.coef_, intercept = plain_sgd(self.coef_,
                                          self.intercept_[0],
                                          loss_function,
                                          penalty_type,
                                          alpha, C,
                                          self.l1_ratio,
                                          dataset,
                                          n_iter,
                                          int(self.fit_intercept),
                                          int(self.verbose),
                                          int(self.shuffle),
                                          self.random_state,
                                          1.0, 1.0,
                                          learning_rate_type,
                                          self.eta0, self.power_t, self.t_,
                                          intercept_decay)

        self.intercept_ = np.atleast_1d(intercept)


class SGDRegressor(BaseSGDRegressor, _LearntSelectorMixin):
    """Linear model fitted by minimizing a regularized empirical loss with SGD

    SGD stands for Stochastic Gradient Descent: the gradient of the loss is
    estimated each sample at a time and the model is updated along the way with
    a decreasing strength schedule (aka learning rate).

    The regularizer is a penalty added to the loss function that shrinks model
    parameters towards the zero vector using either the squared euclidean norm
    L2 or the absolute norm L1 or a combination of both (Elastic Net). If the
    parameter update crosses the 0.0 value because of the regularizer, the
    update is truncated to 0.0 to allow for learning sparse models and achieve
    online feature selection.

    This implementation works with data represented as dense numpy arrays of
    floating point values for the features.

    Parameters
    ----------
    loss : str, 'squared_loss', 'huber', 'epsilon_insensitive', \
                or 'squared_epsilon_insensitive'
        The loss function to be used. Defaults to 'squared_loss' which refers
        to the ordinary least squares fit. 'huber' modifies 'squared_loss' to
        focus less on getting outliers correct by switching from squared to
        linear loss past a distance of epsilon. 'epsilon_insensitive' ignores
        errors less than epsilon and is linear past that; this is the loss
        function used in SVR. 'squared_epsilon_insensitive' is the same but
        becomes squared loss past a tolerance of epsilon.

    penalty : str, 'l2' or 'l1' or 'elasticnet'
        The penalty (aka regularization term) to be used. Defaults to 'l2'
        which is the standard regularizer for linear SVM models. 'l1' and
        'elasticnet' migh bring sparsity to the model (feature selection)
        not achievable with 'l2'.

    alpha : float
        Constant that multiplies the regularization term. Defaults to 0.0001

    l1_ratio : float
        The Elastic Net mixing parameter, with 0 <= l1_ratio <= 1.
        l1_ratio=0 corresponds to L2 penalty, l1_ratio=1 to L1.
        Defaults to 0.15.

    fit_intercept: bool
        Whether the intercept should be estimated or not. If False, the
        data is assumed to be already centered. Defaults to True.

    n_iter: int, optional
        The number of passes over the training data (aka epochs).
        Defaults to 5.

    shuffle: bool, optional
        Whether or not the training data should be shuffled after each epoch.
        Defaults to False.

    random_state: int seed, RandomState instance, or None (default)
        The seed of the pseudo random number generator to use when
        shuffling the data.

    verbose: integer, optional
        The verbosity level.

    epsilon: float
        Epsilon in the epsilon-insensitive loss functions; only if `loss` is
        'huber', 'epsilon_insensitive', or 'squared_epsilon_insensitive'.
        For 'huber', determines the threshold at which it becomes less
        important to get the prediction exactly right.
        For epsilon-insensitive, any differences between the current prediction
        and the correct label are ignored if they are less than this threshold.

    learning_rate : string, optional
        The learning rate:
        constant: eta = eta0
        optimal: eta = 1.0/(t+t0)
        invscaling: eta = eta0 / pow(t, power_t) [default]

    eta0 : double, optional
        The initial learning rate [default 0.01].

    power_t : double, optional
        The exponent for inverse scaling learning rate [default 0.25].

    warm_start : bool, optional
        When set to True, reuse the solution of the previous call to fit as
        initialization, otherwise, just erase the previous solution.

    Attributes
    ----------
    `coef_` : array, shape = [n_features]
        Weights asigned to the features.

    `intercept_` : array, shape = [1]
        The intercept term.

    Examples
    --------
    >>> import numpy as np
    >>> from sklearn import linear_model
    >>> n_samples, n_features = 10, 5
    >>> np.random.seed(0)
    >>> y = np.random.randn(n_samples)
    >>> X = np.random.randn(n_samples, n_features)
    >>> clf = linear_model.SGDRegressor()
    >>> clf.fit(X, y)
    SGDRegressor(alpha=0.0001, epsilon=0.1, eta0=0.01, fit_intercept=True,
           l1_ratio=0.15, learning_rate='invscaling', loss='squared_loss',
           n_iter=5, penalty='l2', power_t=0.25, random_state=None,
           shuffle=False, verbose=0, warm_start=False)

    See also
    --------
    Ridge, ElasticNet, Lasso, SVR

    """
    def __init__(self, loss="squared_loss", penalty="l2", alpha=0.0001,
                 l1_ratio=0.15, fit_intercept=True, n_iter=5, shuffle=False,
                 verbose=0, epsilon=DEFAULT_EPSILON, random_state=None,
                 learning_rate="invscaling", eta0=0.01, power_t=0.25,
                 warm_start=False):
        super(SGDRegressor, self).__init__(loss=loss, penalty=penalty,
                                           alpha=alpha, l1_ratio=l1_ratio,
                                           fit_intercept=fit_intercept,
                                           n_iter=n_iter, shuffle=shuffle,
                                           verbose=verbose,
                                           epsilon=epsilon,
                                           random_state=random_state,
                                           learning_rate=learning_rate,
                                           eta0=eta0, power_t=power_t,
                                           warm_start=warm_start)
