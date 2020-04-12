import warnings

import numpy as np
from sklearn.base import is_classifier
from sklearn.exceptions import ConvergenceWarning
from sklearn.model_selection import train_test_split
from sklearn.neural_network import MLPClassifier
from sklearn.neural_network._base import LOSS_FUNCTIONS
from sklearn.neural_network._stochastic_optimizers import AdamOptimizer, SGDOptimizer
from sklearn.utils import gen_batches, shuffle


class ModifiedMLPClassifier(MLPClassifier):
    def __init__(
        self,
        hidden_layer_sizes=(100,),
        activation="relu",
        solver="adam",
        alpha=0.0001,
        batch_size="auto",
        learning_rate="constant",
        learning_rate_init=0.001,
        power_t=0.5,
        max_iter=200,
        shuffle=True,
        random_state=None,
        tol=1e-4,
        verbose=False,
        warm_start=False,
        momentum=0.9,
        nesterovs_momentum=True,
        early_stopping=False,
        validation_fraction=0.1,
        beta_1=0.9,
        beta_2=0.999,
        epsilon=1e-8,
        n_iter_no_change=10,
        max_fun=15000,
        custom_validation_data=None,
    ):
        super().__init__(
            hidden_layer_sizes=hidden_layer_sizes,
            activation=activation,
            solver=solver,
            alpha=alpha,
            batch_size=batch_size,
            learning_rate=learning_rate,
            learning_rate_init=learning_rate_init,
            power_t=power_t,
            max_iter=max_iter,
            shuffle=shuffle,
            random_state=random_state,
            tol=tol,
            verbose=verbose,
            warm_start=warm_start,
            momentum=momentum,
            nesterovs_momentum=nesterovs_momentum,
            early_stopping=early_stopping,
            validation_fraction=validation_fraction,
            beta_1=beta_1,
            beta_2=beta_2,
            epsilon=epsilon,
            n_iter_no_change=n_iter_no_change,
            max_fun=max_fun,
        )
        self.custom_validation_data = custom_validation_data
        self.validation_loss_curve_ = []

    def _fit_stochastic(
        self,
        X,
        y,
        activations,
        deltas,
        coef_grads,
        intercept_grads,
        layer_units,
        incremental,
    ):

        if not incremental or not hasattr(self, "_optimizer"):
            params = self.coefs_ + self.intercepts_

            if self.solver == "sgd":
                self._optimizer = SGDOptimizer(
                    params,
                    self.learning_rate_init,
                    self.learning_rate,
                    self.momentum,
                    self.nesterovs_momentum,
                    self.power_t,
                )
            elif self.solver == "adam":
                self._optimizer = AdamOptimizer(
                    params,
                    self.learning_rate_init,
                    self.beta_1,
                    self.beta_2,
                    self.epsilon,
                )

        # early_stopping in partial_fit doesn't make sense
        early_stopping = self.early_stopping and not incremental
        if early_stopping:
            # don't stratify in multilabel classification
            should_stratify = is_classifier(self) and self.n_outputs_ == 1
            stratify = y if should_stratify else None
            X, X_val, y, y_val = train_test_split(
                X,
                y,
                random_state=self._random_state,
                test_size=self.validation_fraction,
                stratify=stratify,
            )
            if is_classifier(self):
                y_val = self._label_binarizer.inverse_transform(y_val)
        else:
            X_val = None
            y_val = None

        n_samples = X.shape[0]

        if self.batch_size == "auto":
            batch_size = min(200, n_samples)
        else:
            batch_size = np.clip(self.batch_size, 1, n_samples)

        try:
            for it in range(self.max_iter):
                if self.shuffle:
                    X, y = shuffle(X, y, random_state=self._random_state)
                accumulated_loss = 0.0
                for batch_slice in gen_batches(n_samples, batch_size):
                    activations[0] = X[batch_slice]
                    batch_loss, coef_grads, intercept_grads = self._backprop(
                        X[batch_slice],
                        y[batch_slice],
                        activations,
                        deltas,
                        coef_grads,
                        intercept_grads,
                    )
                    accumulated_loss += batch_loss * (
                        batch_slice.stop - batch_slice.start
                    )

                    # update weights
                    grads = coef_grads + intercept_grads
                    self._optimizer.update_params(grads)

                self.n_iter_ += 1
                self.loss_ = accumulated_loss / X.shape[0]

                self.t_ += n_samples
                self.loss_curve_.append(self.loss_)

                # --------------------------- #
                #   Custom validation check   #
                # --------------------------- #

                if self.custom_validation_data is not None:
                    custom_X_valid = self.custom_validation_data[0]
                    custom_y_valid = self.custom_validation_data[1]

                    if type(custom_X_valid) is not np.ndarray:
                        custom_X_valid = custom_X_valid.to_numpy()
                    if type(custom_y_valid) is not np.ndarray:
                        custom_y_valid = custom_y_valid.to_numpy()

                    # Get validation loss
                    # Code adapted from self._backprop()
                    loss_func_name = self.loss
                    if (
                        loss_func_name == "log_loss"
                        and self.out_activation_ == "logistic"
                    ):
                        loss_func_name = "binary_log_loss"

                    valid_loss = LOSS_FUNCTIONS[loss_func_name](
                        custom_y_valid, self._predict(custom_X_valid)
                    )

                    # Add L2 regularization term to loss
                    temp_values = np.sum(
                        np.array([np.dot(s.ravel(), s.ravel()) for s in self.coefs_])
                    )
                    valid_loss += (
                        (0.5 * self.alpha) * temp_values / custom_X_valid.shape[0]
                    )

                    self.validation_loss_curve_.append(valid_loss)
                    verbose_msg = (
                        "Iteration %d, training loss = %.8f, validation loss = %.8f"
                        % (self.n_iter_, self.loss_, valid_loss)
                    )

                else:
                    valid_loss = None
                    verbose_msg = "Iteration %d, training loss = %.8f" % (
                        self.n_iter_,
                        self.loss_,
                    )

                # --------------------------- #
                #       Custom code end       #
                # --------------------------- #

                if self.verbose:
                    print(verbose_msg)

                # update no_improvement_count based on training loss or
                # validation score according to early_stopping
                self._update_no_improvement_count(early_stopping, X_val, y_val)

                # for learning rate that needs to be updated at iteration end
                self._optimizer.iteration_ends(self.t_)

                if self._no_improvement_count > self.n_iter_no_change:
                    # not better than last `n_iter_no_change` iterations by tol
                    # stop or decrease learning rate
                    if early_stopping:
                        msg = (
                            "Validation score did not improve more than "
                            "tol=%f for %d consecutive epochs."
                            % (self.tol, self.n_iter_no_change)
                        )
                    else:
                        msg = (
                            "Training loss did not improve more than tol=%f"
                            " for %d consecutive epochs."
                            % (self.tol, self.n_iter_no_change)
                        )

                    is_stopping = self._optimizer.trigger_stopping(msg, self.verbose)
                    if is_stopping:
                        break
                    else:
                        self._no_improvement_count = 0

                if incremental:
                    break

                if self.n_iter_ == self.max_iter:
                    warnings.warn(
                        "Stochastic Optimizer: Maximum iterations (%d) "
                        "reached and the optimization hasn't converged yet."
                        % self.max_iter,
                        ConvergenceWarning,
                    )
        except KeyboardInterrupt:
            warnings.warn("Training interrupted by user.")

        if early_stopping:
            # restore best weights
            self.coefs_ = self._best_coefs
            self.intercepts_ = self._best_intercepts
