"""Utilities to evaluate models with respect to a variable
"""
# Author: Alexander Fabisch <afabisch@informatik.uni-bremen.de>
#
# License: BSD 3 clause

import numpy as np
import warnings
from .base import is_classifier, clone
from .cross_validation import _check_cv
from .utils import check_arrays
from .externals.joblib import Parallel, delayed
from .metrics.scorer import get_scorer
from .grid_search import _check_scorable, _split, _fit, _score


def learning_curve(estimator, X, y, train_sizes=np.linspace(0.1, 1.0, 10),
                   cv=None, scoring=None, exploit_incremental_learning=False,
                   n_jobs=1, pre_dispatch="all", verbose=0):
    """Learning curve

    Determines cross-validated training and test scores for different training
    set sizes.

    A cross-validation generator splits the whole dataset k times in training
    and test data. Subsets of the training set with varying sizes will be used
    to train the estimator and a score for each training subset size and the
    test set will be computed. Afterwards, the scores will be averaged over
    all k runs for each training subset size.

    Parameters
    ----------
    estimator : object type that implements the "fit" and "predict" methods
        An object of that type which is cloned for each validation.

    X : array-like, shape (n_samples, n_features)
        Training vector, where n_samples is the number of samples and
        n_features is the number of features.

    y : array-like, shape (n_samples) or (n_samples, n_features), optional
        Target relative to X for classification or regression;
        None for unsupervised learning.

    train_sizes : array-like, shape = (n_ticks,), dtype float or int
        Relative or absolute numbers of training examples that will be used to
        generate the learning curve. If the dtype is float, it is regarded as a
        fraction of the maximum size of the training set (that is determined
        by the selected validation method), i.e. it has to be within (0, 1].
        Otherwise it is interpreted as absolute sizes of the training sets.
        Note that for classification the number of samples usually have to
        be big enough to contain at least one sample from each class.
        (default: np.linspace(0.1, 1.0, 10))

    cv : integer, cross-validation generator, optional
        If an integer is passed, it is the number of folds (defaults to 3).
        Specific cross-validation objects can be passed, see
        sklearn.cross_validation module for the list of possible objects

    scoring : string, callable or None, optional, default: None
        A string (see model evaluation documentation) or
        a scorer callable object / function with signature
        ``scorer(estimator, X, y)``.

    exploit_incremental_learning : boolean, optional, default: False
        If the estimator supports incremental learning, this will be
        used to speed up fitting for different training set sizes.

    n_jobs : integer, optional
        Number of jobs to run in parallel (default 1).

    pre_dispatch : integer or string, optional
        Number of predispatched jobs for parallel execution (default is
        all). The option can reduce the allocated memory. The string can
        be an expression like '2*n_jobs'.

    verbose : integer, optional
        Controls the verbosity: the higher, the more messages.

    Returns
    -------
    train_sizes_abs : array, shape = [n_unique_ticks,], dtype int
        Numbers of training examples that has been used to generate the
        learning curve. Note that the number of ticks might be less
        than n_ticks because duplicate entries will be removed.

    train_scores : array, shape = [n_ticks,]
        Scores on training sets.

    test_scores : array, shape = [n_ticks,]
        Scores on test set.

    Notes
    -----
    See :ref:`examples/plot_learning_curve.py <example_plot_learning_curve.py>`
    """

    if exploit_incremental_learning and not hasattr(estimator, "partial_fit"):
        raise ValueError("An estimator must support the partial_fit interface "
                         "to exploit incremental learning")

    X, y = check_arrays(X, y, sparse_format='csr', allow_lists=True)
    # Make a list since we will be iterating multiple times over the folds
    cv = list(_check_cv(cv, X, y, classifier=is_classifier(estimator)))

    # HACK as long as boolean indices are allowed in cv generators
    if cv[0][0].dtype == bool:
        new_cv = []
        for i in range(len(cv)):
            new_cv.append((np.nonzero(cv[i][0])[0], np.nonzero(cv[i][1])[0]))
        cv = new_cv

    n_max_training_samples = len(cv[0][0])
    # Because the lengths of folds can be significantly different, it is
    # not guaranteed that we use all of the available training data when we
    # use the first 'n_max_training_samples' samples.
    train_sizes_abs = _translate_train_sizes(train_sizes,
                                             n_max_training_samples)
    n_unique_ticks = train_sizes_abs.shape[0]
    if verbose > 0:
        print("[learning_curve] Training set sizes: " + str(train_sizes_abs))

    _check_scorable(estimator, scoring=scoring)
    scorer = get_scorer(scoring)

    parallel = Parallel(n_jobs=n_jobs, pre_dispatch=pre_dispatch,
                        verbose=verbose)
    if exploit_incremental_learning:
        if is_classifier(estimator):
            classes = np.unique(y)
        else:
            classes = None
        out = parallel(delayed(_incremental_fit_estimator)(
            estimator, X, y, classes, train, test, train_sizes_abs, scorer,
            verbose) for train, test in cv)
    else:
        out = parallel(delayed(_fit_estimator)(
            estimator, X, y, train, test, n_train_samples, scorer, verbose)
            for train, test in cv for n_train_samples in train_sizes_abs)
        out = np.array(out)
        n_cv_folds = out.shape[0]/n_unique_ticks
        out = out.reshape(n_cv_folds, n_unique_ticks, 2)

    avg_over_cv = np.asarray(out).mean(axis=0).reshape(n_unique_ticks, 2)

    return train_sizes_abs, avg_over_cv[:, 0], avg_over_cv[:, 1]


def _translate_train_sizes(train_sizes, n_max_training_samples):
    """Determine absolute sizes of training subsets and validate 'train_sizes'.

    Examples:
        _translate_train_sizes([0.5, 1.0], 10) -> [5, 10]
        _translate_train_sizes([5, 10], 10) -> [5, 10]

    Parameters
    ----------
    train_sizes : array-like, shape = (n_ticks,), dtype float or int
        Numbers of training examples that will be used to generate the
        learning curve. If the dtype is float, it is regarded as a
        fraction of 'n_max_training_samples', i.e. it has to be within (0, 1].

    n_max_training_samples : int
        Maximum number of training samples (upper bound of 'train_sizes').

    Returns
    -------
    train_sizes_abs : array, shape = [n_unique_ticks,], dtype int
        Numbers of training examples that will be used to generate the
        learning curve. Note that the number of ticks might be less
        than n_ticks because duplicate entries will be removed.
    """
    train_sizes_abs = np.asarray(train_sizes)
    n_ticks = train_sizes_abs.shape[0]
    n_min_required_samples = np.min(train_sizes_abs)
    n_max_required_samples = np.max(train_sizes_abs)
    if np.issubdtype(train_sizes_abs.dtype, np.float):
        if n_min_required_samples <= 0.0 or n_max_required_samples > 1.0:
            raise ValueError("train_sizes has been interpreted as fractions "
                             "of the maximum number of training samples and "
                             "must be within (0, 1], but is within [%f, %f]."
                             % (n_min_required_samples,
                                n_max_required_samples))
        train_sizes_abs = (train_sizes_abs
                           * n_max_training_samples).astype(np.int)
        train_sizes_abs = np.clip(train_sizes_abs, 1,
                                  n_max_training_samples)
    else:
        if (n_min_required_samples <= 0 or
                n_max_required_samples > n_max_training_samples):
            raise ValueError("train_sizes has been interpreted as absolute "
                             "numbers of training samples and must be within "
                             "(0, %d], but is within [%d, %d]."
                             % (n_max_training_samples,
                                n_min_required_samples,
                                n_max_required_samples))

    train_sizes_abs = np.unique(train_sizes_abs)
    if n_ticks > train_sizes_abs.shape[0]:
        warnings.warn("Removed duplicate entries from 'train_sizes'. Number "
                      "of ticks will be less than than the size of "
                      "'train_sizes' %d instead of %d)."
                      % (train_sizes_abs.shape[0], n_ticks), RuntimeWarning)

    return train_sizes_abs


def _fit_estimator(base_estimator, X, y, train, test,
                   n_train_samples, scorer, verbose):
    """Train estimator on a training subset and compute scores."""
    train_subset = train[:n_train_samples]
    estimator = clone(base_estimator)
    X_train, y_train = _split(estimator, X, y, train_subset)
    X_test, y_test = _split(estimator, X, y, test, train_subset)
    _fit(estimator.fit, X_train, y_train)
    train_score = _score(estimator, X_train, y_train, scorer)
    test_score = _score(estimator, X_test, y_test, scorer)
    return train_score, test_score


def _incremental_fit_estimator(base_estimator, X, y, classes, train, test,
                               train_sizes, scorer, verbose):
    """Train estimator on training subsets incrementally and compute scores."""
    estimator = clone(base_estimator)
    train_scores, test_scores = [], []
    partitions = zip(train_sizes, np.split(train, train_sizes)[:-1])
    for n_train_samples, partial_train in partitions:
        X_train, y_train = _split(estimator, X, y, train[:n_train_samples])
        X_partial_train, y_partial_train = _split(estimator, X, y,
                                                  partial_train)
        X_test, y_test = _split(estimator, X, y, test, train[:n_train_samples])
        _fit(estimator.partial_fit, X_partial_train, y_partial_train,
             classes=classes)
        train_scores.append(_score(estimator, X_train, y_train, scorer))
        test_scores.append(_score(estimator, X_test, y_test, scorer))
    return np.array((train_scores, test_scores)).T
