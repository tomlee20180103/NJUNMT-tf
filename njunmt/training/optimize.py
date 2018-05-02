# Copyright 2017 Natural Language Processing Group, Nanjing University, zhaocq.nlp@gmail.com.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
""" Define a wrapper class for optimizer and optimize function. """
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import sys
import tensorflow as tf

from njunmt.utils.lr_decay import create_learning_rate_decay_fn
from njunmt.utils.misc import add_dict_to_collection
from njunmt.utils.configurable import Configurable
from njunmt.utils.constants import ModeKeys
from njunmt.utils.constants import Constants


def _get_optimizer(name, **params):
    """ Create optimizer.

    Args:
        name: A string, the name of the optimizer.
        **params: A dictionary of optimizer parameters.

    Returns: A Tensorflow optimizer.

    Raises:
        ValueError: if `name` is unknown.
    """
    if name in tf.contrib.layers.OPTIMIZER_CLS_NAMES.keys():
        return tf.contrib.layers.OPTIMIZER_CLS_NAMES[name](**params)
    if name == "LazyAdam":
        return tf.contrib.opt.LazyAdamOptimizer(**params)
    if name == "Adadelta":
        return tf.train.AdadeltaOptimizer(**params)
    raise ValueError("Unknown optimizer name: {}".format(name))


def average_gradients(tower_grads):
    """Calculate the average gradient for each shared variable across all towers.
    Note that this function provides a synchronization point across all towers.
    Args:
        tower_grads: List of lists of (gradient, variable) tuples. The outer list
        is over individual gradients. The inner list is over the gradient
        calculation for each tower.
    Returns:
        List of pairs of (gradient, variable) where the gradient has been averaged
        across all towers.
    """
    average_grads = []
    for grad_and_vars in zip(*tower_grads):
        # Note that each grad_and_vars looks like the following:
        #   ((grad0_gpu0, var0_gpu0), ... , (grad0_gpuN, var0_gpuN))
        grads = []
        for g, _ in grad_and_vars:
            # Add 0 dimension to the gradients to represent the tower.
            expanded_g = tf.expand_dims(g, 0)

            # Append on a 'tower' dimension which we will average over below.
            grads.append(expanded_g)
        # Average over the 'tower' dimension.
        grad = tf.concat(axis=0, values=grads)
        grad = tf.reduce_mean(grad, 0)

        # Keep in mind that the Variables are redundant because they are shared
        # across towers. So .. we will just return the first tower's pointer to
        # the Variable.
        v = grad_and_vars[0][1]
        grad_and_var = (grad, v)
        average_grads.append(grad_and_var)
    return average_grads


class OptimizerWrapper(Configurable):
    """ Define the wrapper class for creating optimizer. """

    def __init__(self, params):
        """ Initializes the parameters of the optimizer.

        Args:
            params: A dictionary of the parameters of the optimizer.
        """
        super(OptimizerWrapper, self).__init__(
            params=params, mode=ModeKeys.TRAIN,
            name=None, verbose=True)
        self._optimizer = self._create_optimizer()

    @property
    def optimizer(self):
        """ Returns the optimizer. """
        return self._optimizer

    @staticmethod
    def default_params():
        """ Returns a dictionary of default parameters of the optimizer. """
        return {
            "optimizer.name": "Adam",
            "optimizer.learning_rate": 1e-4,
            "optimizer.params": {},  # Arbitrary parameters for the optimizer
            "optimizer.lr_decay": {
                "decay_type": None,
                "decay_steps": 100,
                "decay_rate": 0.99,
                "start_decay_at": 0,
                "stop_decay_at": sys.maxsize,
                "min_learning_rate": 1.0e-9,
                "staircase": False,
                "patience": None,  # for loss_decay
                "dmodel": None,  # for noam_decay
                "scale": 2.0  # for noam_decay
            },
            "optimizer.clip_gradients": 1.0,
            "optimizer.sync_replicas": 0,
            "optimizer.sync_replicas_to_aggregate": 0,
        }

    def _create_optimizer(self):
        """ Creates the optimizer. """
        learning_rate = tf.get_variable(
            Constants.LEARNING_RATE_VAR_NAME,
            shape=(), dtype=tf.float32,
            initializer=tf.constant_initializer(
                value=self.params["optimizer.learning_rate"],
                dtype=tf.float32),
            trainable=False)
        name = self.params["optimizer.name"]
        tf.logging.info("use %s optimizer with initial learning rate=%f"
                        % (name, self.params["optimizer.learning_rate"]))

        global_step_tensor = tf.train.get_or_create_global_step()
        # create decay fn
        decay_fn = create_learning_rate_decay_fn(**self.params["optimizer.lr_decay"])
        other_tensor_dict = {}
        if decay_fn:  # apply learning rate decay
            learning_rate, other_tensor_dict = decay_fn(learning_rate, global_step_tensor)
        # add to collections
        other_tensor_dict[Constants.LEARNING_RATE_VAR_NAME] = learning_rate
        add_dict_to_collection(Constants.LEARNING_RATE_VAR_NAME, other_tensor_dict)
        tf.add_to_collection(Constants.DISPLAY_KEY_COLLECTION_NAME, "training_stats/learning_rate")
        tf.add_to_collection(Constants.DISPLAY_VALUE_COLLECTION_NAME, learning_rate)
        # create optimizer
        optimizer = _get_optimizer(name, learning_rate=learning_rate,
                                   **self.params["optimizer.params"])
        return optimizer

    def optimize(self,
                 loss,
                 grads_and_vars=None):
        """ Creates the optimizer with learning rate decaying, optimizes
        loss and return a train_op.

        Args:
            loss: A list of loss Tensors.
            variables: A list of variables to optimize or None to use all trainable variables.
            grads_and_vars: A list of (gradients, variables) to be averaged.

        Returns: The train_op.
        """
        assert len(loss) == len(grads_and_vars)

        def _clip_gradients(grads_and_vars):
            """Clips gradients by global norm."""
            gradients, variables = zip(*grads_and_vars)
            clipped_gradients, _ = tf.clip_by_global_norm(
                gradients, self.params["optimizer.clip_gradients"])
            return list(zip(clipped_gradients, variables))

        # average gradients
        # [[(var0, grad0_0), (var1, grad1_0), ...], [(var0, grad0_1, var1, grad1_1), ...], ...]
        with tf.variable_scope("OptimizeLoss"):
            if len(loss) == 1:
                loss = loss[0]
                grads_and_vars = grads_and_vars[0]
            else:
                grads_and_vars = average_gradients(grads_and_vars)
            if self.params["optimizer.clip_gradients"] > 0:
                grads_and_vars = _clip_gradients(grads_and_vars)
            # Create gradient updates.
            grad_updates = self._optimizer.apply_gradients(
                grads_and_vars,
                global_step=tf.train.get_global_step(),
                name="train")
        return grad_updates


