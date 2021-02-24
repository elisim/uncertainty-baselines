# coding=utf-8
# Copyright 2021 The Uncertainty Baselines Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Wide ResNet 28-10 with Monte Carlo dropout on CIFAR-10."""

import functools
import os
import time
from absl import app
from absl import flags
from absl import logging

import tensorflow as tf
import tensorflow_datasets as tfds
import uncertainty_baselines as ub
import utils  # local file import
import uncertainty_metrics as um

flags.DEFINE_integer('seed', 42, 'Random seed.')
flags.DEFINE_integer('per_core_batch_size', 64, 'Batch size per TPU core/GPU.')
flags.DEFINE_float(
    'base_learning_rate', 0.1,
    'Base learning rate when total batch size is 128. It is '
    'scaled by the ratio of the total batch size to 128.')
flags.DEFINE_integer(
    'lr_warmup_epochs', 1,
    'Number of epochs for a linear warmup to the initial '
    'learning rate. Use 0 to do no warmup.')
flags.DEFINE_float('lr_decay_ratio', 0.2, 'Amount to decay learning rate.')
flags.DEFINE_list('lr_decay_epochs', ['60', '120', '160'],
                  'Epochs to decay learning rate by.')
flags.DEFINE_float('l2', 3e-4, 'L2 regularization coefficient.')
flags.DEFINE_float('dropout_rate', 0.1, 'Dropout rate.')
flags.DEFINE_integer('num_dropout_samples', 1,
                     'Number of dropout samples to use for prediction.')
flags.DEFINE_integer('num_dropout_samples_training', 1,
                     'Number of dropout samples for training.')
flags.DEFINE_bool(
    'filterwise_dropout', False, 'Dropout whole convolutional'
    'filters instead of individual values in the feature map.')
flags.DEFINE_bool(
    'residual_dropout', True,
    'Apply dropout only to the residual connections as proposed'
    'in the original paper.'
    'Otherwise dropout is applied after every layer.')

flags.DEFINE_enum(
    'dataset', 'cifar10', enum_values=['cifar10', 'cifar100'], help='Dataset.')
# TODO(ghassen): consider adding CIFAR-100-C to TFDS.
flags.DEFINE_string(
    'cifar100_c_path', None,
    'Path to the TFRecords files for CIFAR-100-C. Only valid '
    '(and required) if dataset is cifar100 and corruptions.')
flags.DEFINE_integer(
    'corruptions_interval', -1,
    'Number of epochs between evaluating on the corrupted '
    'test data. Use -1 to never evaluate.')
flags.DEFINE_integer(
    'checkpoint_interval', -1,
    'Number of epochs between saving checkpoints. Use -1 to '
    'never save checkpoints.')
flags.DEFINE_integer('num_bins', 15, 'Number of bins for ECE.')
flags.DEFINE_string('output_dir', '/tmp/cifar', 'Output directory.')
flags.DEFINE_integer('train_epochs', 200, 'Number of training epochs.')

# Accelerator flags.
flags.DEFINE_bool('use_gpu', False, 'Whether to run on GPU or otherwise TPU.')
flags.DEFINE_bool('use_bfloat16', False, 'Whether to use mixed precision.')
flags.DEFINE_integer('num_cores', 8, 'Number of TPU cores or number of GPUs.')
flags.DEFINE_string('tpu', None,
                    'Name of the TPU. Only used if use_gpu is False.')

# CondConv Flags
flags.DEFINE_integer('num_experts', 4, 'Number of experts to aggregate over.')
flags.DEFINE_bool('use_cond_dense', True, 'Whether to use CondDense.')
flags.DEFINE_bool('reduce_dense_outputs', False,
                  'Whether to aggregate on kernels or outputs.')
flags.DEFINE_enum(
    'loss',
    'gibbs_ce',
    enum_values=[
        'gibbs_ce', 'unweighted_gibbs_ce', 'moe', 'unweighted_moe', 'poe',
        'unweighted_poe'
    ],
    help='Choice of loss function/consensus algorithm')

flags.DEFINE_enum(
    'cond_placement',
    'dropout',
    enum_values=['dropout', 'all', 'none'],
    help='Where to place the CondConv layers.')

# TODO(ghassen): consider separating the CondDense and CondConv routing flags.
flags.DEFINE_enum(
    'routing_fn',
    'sigmoid',
    enum_values=[
        'sigmoid', 'softmax', 'noisy_softmax', 'onehot_top_k',
        'noisy_onehot_top_k', 'softmax_top_k', 'noisy_softmax_top_k'
    ],
    help='The choice of routing function for CondConv and CondDense.')
flags.DEFINE_bool('normalize_routing', False,
                  'Whether to normalize CondConv routing weights.')
flags.DEFINE_bool('normalize_dense_routing', False,
                  'Whether to normalize the final CondDense routing weights.')
flags.DEFINE_enum(
    'routing_pooling',
    'global_average',
    enum_values=[
        'global_average', 'global_max', 'average_8', 'max_8', 'flatten'
    ],
    help='Type of pooling to apply to the inputs of the routing functions.')
flags.DEFINE_integer('top_k', -1, 'The number of experts to select from.')

flags.DEFINE_integer('resnet_width_multiplier', 5,
                     'WideResNet width multiplier.')

FLAGS = flags.FLAGS


def main(argv):
  del argv  # unused arg
  tf.io.gfile.makedirs(FLAGS.output_dir)
  logging.info('Saving checkpoints at %s', FLAGS.output_dir)
  tf.random.set_seed(FLAGS.seed)

  if FLAGS.use_gpu:
    logging.info('Use GPU')
    strategy = tf.distribute.MirroredStrategy()
  else:
    logging.info('Use TPU at %s',
                 FLAGS.tpu if FLAGS.tpu is not None else 'local')
    resolver = tf.distribute.cluster_resolver.TPUClusterResolver(tpu=FLAGS.tpu)
    tf.config.experimental_connect_to_cluster(resolver)
    tf.tpu.experimental.initialize_tpu_system(resolver)
    strategy = tf.distribute.TPUStrategy(resolver)

  ds_info = tfds.builder(FLAGS.dataset).info
  batch_size = FLAGS.per_core_batch_size * FLAGS.num_cores
  test_batch_size = FLAGS.per_core_batch_size * FLAGS.num_cores
  steps_per_epoch = ds_info.splits['train'].num_examples // batch_size
  steps_per_eval = ds_info.splits['test'].num_examples // test_batch_size
  num_classes = ds_info.features['label'].num_classes

  if FLAGS.dataset == 'cifar10':
    dataset_builder_class = ub.datasets.Cifar10Dataset
  else:
    dataset_builder_class = ub.datasets.Cifar100Dataset
  train_dataset_builder = dataset_builder_class(
      split=tfds.Split.TRAIN,
      use_bfloat16=FLAGS.use_bfloat16)
  train_dataset = train_dataset_builder.load(batch_size=batch_size)
  train_dataset = strategy.experimental_distribute_dataset(train_dataset)
  clean_test_dataset_builder = dataset_builder_class(
      split=tfds.Split.TEST,
      use_bfloat16=FLAGS.use_bfloat16)
  clean_test_dataset = clean_test_dataset_builder.load(
      batch_size=test_batch_size)
  test_datasets = {
      'clean': strategy.experimental_distribute_dataset(clean_test_dataset),
  }
  if FLAGS.corruptions_interval > 0:
    if FLAGS.dataset == 'cifar10':
      load_c_dataset = utils.load_cifar10_c
    else:
      load_c_dataset = functools.partial(
          utils.load_cifar100_c, path=FLAGS.cifar100_c_path)
    corruption_types, max_intensity = utils.load_corrupted_test_info(
        FLAGS.dataset)
    for corruption in corruption_types:
      for intensity in range(1, max_intensity + 1):
        dataset = load_c_dataset(
            corruption_name=corruption,
            corruption_intensity=intensity,
            batch_size=test_batch_size,
            use_bfloat16=FLAGS.use_bfloat16)
        test_datasets['{0}_{1}'.format(corruption, intensity)] = (
            strategy.experimental_distribute_dataset(dataset))

  if FLAGS.use_bfloat16:
    policy = tf.keras.mixed_precision.experimental.Policy('mixed_bfloat16')
    tf.keras.mixed_precision.experimental.set_policy(policy)

  summary_writer = tf.summary.create_file_writer(
      os.path.join(FLAGS.output_dir, 'summaries'))

  with strategy.scope():
    logging.info('Building ResNet model')
    model = ub.models.wide_resnet_condconv(
        input_shape=ds_info.features['image'].shape,
        depth=28,
        width_multiplier=FLAGS.resnet_width_multiplier,
        num_classes=num_classes,
        num_experts=FLAGS.num_experts,
        per_core_batch_size=FLAGS.per_core_batch_size,
        use_cond_dense=FLAGS.use_cond_dense,
        reduce_dense_outputs=FLAGS.reduce_dense_outputs,
        cond_placement=FLAGS.cond_placement,
        routing_fn=FLAGS.routing_fn,
        normalize_routing=FLAGS.normalize_routing,
        normalize_dense_routing=FLAGS.normalize_dense_routing,
        top_k=FLAGS.top_k,
        routing_pooling=FLAGS.routing_pooling,
        l2=FLAGS.l2)
    # reuse_routing=FLAGS.reuse_routing,
    # shared_routing_type=FLAGS.shared_routing_type)
    logging.info('Model input shape: %s', model.input_shape)
    logging.info('Model output shape: %s', model.output_shape)
    logging.info('Model number of weights: %s', model.count_params())
    # Linearly scale learning rate and the decay epochs by vanilla settings.
    base_lr = FLAGS.base_learning_rate * batch_size / 128
    lr_decay_epochs = [(int(start_epoch_str) * FLAGS.train_epochs) // 200
                       for start_epoch_str in FLAGS.lr_decay_epochs]
    lr_schedule = ub.schedules.WarmUpPiecewiseConstantSchedule(
        steps_per_epoch,
        base_lr,
        decay_ratio=FLAGS.lr_decay_ratio,
        decay_epochs=lr_decay_epochs,
        warmup_epochs=FLAGS.lr_warmup_epochs)
    optimizer = tf.keras.optimizers.SGD(
        lr_schedule, momentum=0.9, nesterov=True)
    metrics = {
        'train/negative_log_likelihood': tf.keras.metrics.Mean(),
        'train/accuracy': tf.keras.metrics.SparseCategoricalAccuracy(),
        'train/loss': tf.keras.metrics.Mean(),
        'train/ece': um.ExpectedCalibrationError(num_bins=FLAGS.num_bins),
        'test/negative_log_likelihood': tf.keras.metrics.Mean(),
        'test/accuracy': tf.keras.metrics.SparseCategoricalAccuracy(),
        'test/ece': um.ExpectedCalibrationError(num_bins=FLAGS.num_bins),
    }
    if not FLAGS.reduce_dense_outputs and FLAGS.use_cond_dense:
      metrics.update({
          'test/nll_poe':
              tf.keras.metrics.Mean(),
          'test/nll_moe':
              tf.keras.metrics.Mean(),
          'test/nll_unweighted_poe':
              tf.keras.metrics.Mean(),
          'test/nll_unweighted_moe':
              tf.keras.metrics.Mean(),
          'test/unweighted_gibbs_ce':
              tf.keras.metrics.Mean(),
          'test/ece_unweighted_moe':
              um.ExpectedCalibrationError(num_bins=FLAGS.num_bins),
          'test/accuracy_unweighted_moe':
              tf.keras.metrics.SparseCategoricalAccuracy(),
          'test/ece_poe':
              um.ExpectedCalibrationError(num_bins=FLAGS.num_bins),
          'test/accuracy_poe':
              tf.keras.metrics.SparseCategoricalAccuracy(),
          'test/ece_unweighted_poe':
              um.ExpectedCalibrationError(num_bins=FLAGS.num_bins),
          'test/accuracy_unweighted_poe':
              tf.keras.metrics.SparseCategoricalAccuracy(),
      })
      for idx in range(FLAGS.num_experts):
        metrics['test/dense_routing_weight_{}'.format(
            idx)] = tf.keras.metrics.Mean()
        metrics['test/dense_routing_weight_normalized_{}'.format(
            idx)] = tf.keras.metrics.Mean()

    if FLAGS.corruptions_interval > 0:
      corrupt_metrics = {}
      for intensity in range(1, max_intensity + 1):
        for corruption in corruption_types:
          dataset_name = '{0}_{1}'.format(corruption, intensity)
          corrupt_metrics['test/nll_{}'.format(dataset_name)] = (
              tf.keras.metrics.Mean())
          corrupt_metrics['test/accuracy_{}'.format(dataset_name)] = (
              tf.keras.metrics.SparseCategoricalAccuracy())
          corrupt_metrics['test/ece_{}'.format(dataset_name)] = (
              um.ExpectedCalibrationError(num_bins=FLAGS.num_bins))
          corrupt_metrics['test/nll_weighted_moe_{}'.format(dataset_name)] = (
              tf.keras.metrics.Mean())
          corrupt_metrics['test/accuracy_weighted_moe_{}'.format(
              dataset_name)] = (
                  tf.keras.metrics.SparseCategoricalAccuracy())
          corrupt_metrics['test/ece_weighted_moe_{}'.format(dataset_name)] = (
              um.ExpectedCalibrationError(num_bins=FLAGS.num_bins))

    checkpoint = tf.train.Checkpoint(model=model, optimizer=optimizer)
    latest_checkpoint = tf.train.latest_checkpoint(FLAGS.output_dir)
    initial_epoch = 0
    if latest_checkpoint:
      # checkpoint.restore must be within a strategy.scope() so that optimizer
      # slot variables are mirrored.
      checkpoint.restore(latest_checkpoint)
      logging.info('Loaded checkpoint %s', latest_checkpoint)
      initial_epoch = optimizer.iterations.numpy() // steps_per_epoch

  def _process_3d_logits(logits, routing_weights, labels):
    routing_weights_3d = tf.expand_dims(routing_weights, axis=-1)
    weighted_logits = tf.math.reduce_mean(routing_weights_3d * logits, axis=1)
    unweighted_logits = tf.math.reduce_mean(logits, axis=1)

    probs = tf.nn.softmax(logits)
    unweighted_probs = tf.math.reduce_mean(probs, axis=1)
    weighted_probs = tf.math.reduce_sum(routing_weights_3d * probs, axis=1)

    labels_broadcasted = tf.tile(
        tf.reshape(labels, (-1, 1)), (1, FLAGS.num_experts))
    neg_log_likelihoods = tf.keras.losses.sparse_categorical_crossentropy(
        labels_broadcasted, logits, from_logits=True)
    unweighted_gibbs_ce = tf.math.reduce_mean(neg_log_likelihoods)
    weighted_gibbs_ce = tf.math.reduce_mean(
        tf.math.reduce_sum(routing_weights * neg_log_likelihoods, axis=1))
    return {
        'weighted_logits': weighted_logits,
        'unweighted_logits': unweighted_logits,
        'unweighted_probs': unweighted_probs,
        'weighted_probs': weighted_probs,
        'neg_log_likelihoods': neg_log_likelihoods,
        'unweighted_gibbs_ce': unweighted_gibbs_ce,
        'weighted_gibbs_ce': weighted_gibbs_ce
    }

  def _process_3d_logits_train(logits, routing_weights, labels):
    processing_results = _process_3d_logits(logits, routing_weights, labels)
    if FLAGS.loss == 'gibbs_ce':
      probs = processing_results['weighted_probs']
      negative_log_likelihood = processing_results['weighted_gibbs_ce']
    elif FLAGS.loss == 'unweighted_gibbs_ce':
      probs = processing_results['unweighted_probs']
      negative_log_likelihood = processing_results['unweighted_gibbs_ce']
    elif FLAGS.loss == 'moe':
      probs = processing_results['weighted_probs']
      negative_log_likelihood = tf.math.reduce_mean(
          tf.keras.losses.sparse_categorical_crossentropy(
              labels, probs, from_logits=False))
    elif FLAGS.loss == 'unweighted_moe':
      probs = processing_results['unweighted_probs']
      negative_log_likelihood = tf.math.reduce_mean(
          tf.keras.losses.sparse_categorical_crossentropy(
              labels, probs, from_logits=False))
    elif FLAGS.loss == 'poe':
      probs = tf.softmax(processing_results['weighted_logits'])
      negative_log_likelihood = tf.math.reduce_mean(
          tf.keras.losses.sparse_categorical_crossentropy(
              labels, processing_results['weighted_logits'], from_logits=True))
    elif FLAGS.loss == 'unweighted_poe':
      probs = tf.softmax(processing_results['unweighted_logits'])
      negative_log_likelihood = tf.math.reduce_mean(
          tf.keras.losses.sparse_categorical_crossentropy(
              labels, processing_results['unweighted_logits'],
              from_logits=True))
    return probs, negative_log_likelihood

  def _process_3d_logits_test(routing_weights, logits, labels):
    processing_results = _process_3d_logits(logits, routing_weights, labels)
    nll_poe = tf.math.reduce_mean(
        tf.keras.losses.sparse_categorical_crossentropy(
            labels, processing_results['weighted_logits'], from_logits=True))
    nll_unweighted_poe = tf.math.reduce_mean(
        tf.keras.losses.sparse_categorical_crossentropy(
            labels, processing_results['unweighted_logits'], from_logits=True))
    nll_moe = tf.math.reduce_mean(
        tf.keras.losses.sparse_categorical_crossentropy(
            labels, processing_results['weighted_probs'], from_logits=False))
    nll_unweighted_moe = tf.math.reduce_mean(
        tf.keras.losses.sparse_categorical_crossentropy(
            labels, processing_results['unweighted_probs'], from_logits=False))
    return {
        'nll_poe': nll_poe,
        'nll_moe': nll_moe,
        'nll_unweighted_poe': nll_unweighted_poe,
        'nll_unweighted_moe': nll_unweighted_moe,
        'unweighted_gibbs_ce': processing_results['unweighted_gibbs_ce'],
        'weighted_gibbs_ce': processing_results['weighted_gibbs_ce'],
        'weighted_probs': processing_results['weighted_probs'],
        'unweighted_probs': processing_results['unweighted_probs'],
        'weighted_logits': processing_results['weighted_logits'],
        'unweighted_logits': processing_results['unweighted_logits']
    }

  @tf.function
  def train_step(iterator):
    """Training StepFn."""

    def step_fn(inputs):
      """Per-Replica StepFn."""
      images, labels = inputs
      with tf.GradientTape() as tape:
        logits = model(images, training=True)
        if FLAGS.use_bfloat16:
          logits = tf.cast(logits, tf.float32)
        # if not FLAGS.reduce_dense_outputs and FLAGS.use_cond_dense:
        if not isinstance(logits, tuple):
          raise ValueError('Logits are not a tuple.')
        # logits is a `Tensor` of shape [batch_size, num_experts, num_classes]
        logits, all_routing_weights = logits
        # routing_weights is a `Tensor` of shape [batch_size, num_experts]
        routing_weights = all_routing_weights[-1]
        if not FLAGS.reduce_dense_outputs and FLAGS.use_cond_dense:
          probs, negative_log_likelihood = _process_3d_logits_train(
              logits, routing_weights, labels)
        else:
          probs = tf.nn.softmax(logits)
          # Prior to reduce_mean the NLLs are of the shape [batch, num_experts].
          negative_log_likelihood = tf.reduce_mean(
              tf.keras.losses.sparse_categorical_crossentropy(
                  labels, logits, from_logits=True))

        l2_loss = sum(model.losses)
        loss = negative_log_likelihood + l2_loss
        # Scale the loss given the TPUStrategy will reduce sum all gradients.
        scaled_loss = loss / strategy.num_replicas_in_sync

      grads = tape.gradient(scaled_loss, model.trainable_variables)
      optimizer.apply_gradients(zip(grads, model.trainable_variables))

      metrics['train/ece'].update_state(labels, probs)
      metrics['train/loss'].update_state(loss)
      metrics['train/negative_log_likelihood'].update_state(
          negative_log_likelihood)
      metrics['train/accuracy'].update_state(labels, probs)

    strategy.run(step_fn, args=(next(iterator),))

  @tf.function
  def test_step(iterator, dataset_name):
    """Evaluation StepFn."""

    def step_fn(inputs):
      """Per-Replica StepFn."""
      images, labels = inputs
      logits = model(images, training=False)
      if FLAGS.use_bfloat16:
        logits = tf.cast(logits, tf.float32)
      if not isinstance(logits, tuple):
        raise ValueError('Logits not a tuple')
      # logits is a `Tensor` of shape [batch_size, num_experts, num_classes]
      # routing_weights is a `Tensor` of shape [batch_size, num_experts]
      logits, all_routing_weights = logits
      routing_weights = all_routing_weights[-1]
      if not FLAGS.reduce_dense_outputs and FLAGS.use_cond_dense:
        results = _process_3d_logits_test(routing_weights, logits, labels)
      else:
        probs = tf.nn.softmax(logits)
        negative_log_likelihood = tf.reduce_mean(
            tf.keras.losses.sparse_categorical_crossentropy(labels, probs))

      if dataset_name == 'clean':
        if not FLAGS.reduce_dense_outputs and FLAGS.use_cond_dense:
          metrics['test/nll_poe'].update_state(results['nll_poe'])
          metrics['test/nll_moe'].update_state(results['nll_moe'])
          metrics['test/nll_unweighted_poe'].update_state(
              results['nll_unweighted_poe'])
          metrics['test/nll_unweighted_moe'].update_state(
              results['nll_unweighted_moe'])
          metrics['test/unweighted_gibbs_ce'].update_state(
              results['unweighted_gibbs_ce'])
          metrics['test/negative_log_likelihood'].update_state(
              results['weighted_gibbs_ce'])
          metrics['test/ece'].update_state(labels, results['weighted_probs'])
          metrics['test/accuracy'].update_state(labels,
                                                results['weighted_probs'])
          metrics['test/ece_unweighted_moe'].update_state(
              labels, results['unweighted_probs'])
          metrics['test/accuracy_unweighted_moe'].update_state(
              labels, results['unweighted_probs'])
          metrics['test/ece_poe'].update_state(labels,
                                               results['weighted_logits'])
          metrics['test/accuracy_poe'].update_state(labels,
                                                    results['weighted_logits'])
          metrics['test/ece_unweighted_poe'].update_state(
              labels, results['unweighted_logits'])
          metrics['test/accuracy_unweighted_poe'].update_state(
              labels, results['unweighted_logits'])
          # TODO(ghassen): summarize all routing weights not only last layer's.
          average_routing_weights = tf.math.reduce_mean(routing_weights, axis=0)
          routing_weights_sum = tf.math.reduce_sum(average_routing_weights)
          for idx in range(FLAGS.num_experts):
            metrics['test/dense_routing_weight_{}'.format(idx)].update_state(
                average_routing_weights[idx])
            metrics['test/dense_routing_weight_normalized_{}'.format(
                idx)].update_state(average_routing_weights[idx] /
                                   routing_weights_sum)
          # TODO(ghassen): add more metrics for expert utilization,
          # load loss and importance/balance loss.
        else:
          metrics['test/negative_log_likelihood'].update_state(
              negative_log_likelihood)
          metrics['test/accuracy'].update_state(labels, probs)
          metrics['test/ece'].update_state(labels, probs)
      else:
        # TODO(ghassen): figure out how to aggregate probs for the OOD case.
        if not FLAGS.reduce_dense_outputs and FLAGS.use_cond_dense:
          corrupt_metrics['test/nll_{}'.format(dataset_name)].update_state(
              results['unweighted_gibbs_ce'])
          corrupt_metrics['test/accuracy_{}'.format(dataset_name)].update_state(
              labels, results['unweighted_probs'])
          corrupt_metrics['test/ece_{}'.format(dataset_name)].update_state(
              labels, results['unweighted_probs'])

          corrupt_metrics['test/nll_weighted_moe{}'.format(
              dataset_name)].update_state(results['weighted_gibbs_ce'])
          corrupt_metrics['test/accuracy_weighted_moe_{}'.format(
              dataset_name)].update_state(labels, results['weighted_probs'])
          corrupt_metrics['test/ece_weighted_moe{}'.format(
              dataset_name)].update_state(labels, results['weighted_probs'])
        else:
          corrupt_metrics['test/nll_{}'.format(dataset_name)].update_state(
              negative_log_likelihood)
          corrupt_metrics['test/accuracy_{}'.format(dataset_name)].update_state(
              labels, probs)
          corrupt_metrics['test/ece_{}'.format(dataset_name)].update_state(
              labels, probs)

    strategy.run(step_fn, args=(next(iterator),))

  metrics.update({'test/ms_per_example': tf.keras.metrics.Mean()})

  train_iterator = iter(train_dataset)
  start_time = time.time()
  for epoch in range(initial_epoch, FLAGS.train_epochs):
    logging.info('Starting to run epoch: %s', epoch)
    for step in range(steps_per_epoch):
      train_step(train_iterator)

      current_step = epoch * steps_per_epoch + (step + 1)
      max_steps = steps_per_epoch * FLAGS.train_epochs
      time_elapsed = time.time() - start_time
      steps_per_sec = float(current_step) / time_elapsed
      eta_seconds = (max_steps - current_step) / steps_per_sec
      message = ('{:.1%} completion: epoch {:d}/{:d}. {:.1f} steps/s. '
                 'ETA: {:.0f} min. Time elapsed: {:.0f} min'.format(
                     current_step / max_steps, epoch + 1, FLAGS.train_epochs,
                     steps_per_sec, eta_seconds / 60, time_elapsed / 60))
      if step % 20 == 0:
        logging.info(message)

    datasets_to_evaluate = {'clean': test_datasets['clean']}
    if (FLAGS.corruptions_interval > 0 and
        (epoch + 1) % FLAGS.corruptions_interval == 0):
      datasets_to_evaluate = test_datasets
    for dataset_name, test_dataset in datasets_to_evaluate.items():
      test_iterator = iter(test_dataset)
      logging.info('Testing on dataset %s', dataset_name)
      for step in range(steps_per_eval):
        if step % 20 == 0:
          logging.info('Starting to run eval step %s of epoch: %s', step, epoch)
        test_start_time = time.time()
        test_step(test_iterator, dataset_name)
        ms_per_example = (time.time() - test_start_time) * 1e6 / batch_size
        metrics['test/ms_per_example'].update_state(ms_per_example)

      logging.info('Done with testing on %s', dataset_name)

    corrupt_results = {}
    if (FLAGS.corruptions_interval > 0 and
        (epoch + 1) % FLAGS.corruptions_interval == 0):
      corrupt_results = utils.aggregate_corrupt_metrics(corrupt_metrics,
                                                        corruption_types,
                                                        max_intensity)

    logging.info('Train Loss: %.4f, Accuracy: %.2f%%',
                 metrics['train/loss'].result(),
                 metrics['train/accuracy'].result() * 100)
    logging.info('Test NLL: %.4f, Accuracy: %.2f%%',
                 metrics['test/negative_log_likelihood'].result(),
                 metrics['test/accuracy'].result() * 100)
    total_results = {name: metric.result() for name, metric in metrics.items()}
    total_results.update(corrupt_results)
    with summary_writer.as_default():
      for name, result in total_results.items():
        tf.summary.scalar(name, result, step=epoch + 1)

    for metric in metrics.values():
      metric.reset_states()

    if (FLAGS.checkpoint_interval > 0 and
        (epoch + 1) % FLAGS.checkpoint_interval == 0):
      checkpoint_name = checkpoint.save(
          os.path.join(FLAGS.output_dir, 'checkpoint'))
      logging.info('Saved checkpoint to %s', checkpoint_name)
  final_checkpoint_name = checkpoint.save(
      os.path.join(FLAGS.output_dir, 'checkpoint'))
  logging.info('Saved last checkpoint to %s', final_checkpoint_name)


if __name__ == '__main__':
  app.run(main)
