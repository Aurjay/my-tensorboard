# Copyright 2015 The TensorFlow Authors. All Rights Reserved.
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
# ==============================================================================


import os
from unittest import mock

import numpy as np
import tensorflow as tf

from tensorboard import data_compat
from tensorboard import dataclass_compat
from tensorboard.backend.event_processing import plugin_event_accumulator as ea
from tensorboard.compat.proto import config_pb2
from tensorboard.compat.proto import event_pb2
from tensorboard.compat.proto import graph_pb2
from tensorboard.compat.proto import meta_graph_pb2
from tensorboard.compat.proto import summary_pb2
from tensorboard.plugins.audio import metadata as audio_metadata
from tensorboard.plugins.audio import summary as audio_summary
from tensorboard.plugins.graph import metadata as graph_metadata
from tensorboard.plugins.image import metadata as image_metadata
from tensorboard.plugins.image import summary as image_summary
from tensorboard.plugins.scalar import metadata as scalar_metadata
from tensorboard.plugins.scalar import summary as scalar_summary
from tensorboard.util import tb_logging
from tensorboard.util import tensor_util
from tensorboard.util import test_util

logger = tb_logging.get_logger()


class _EventGenerator:
    """Class that can add_events and then yield them back.

    Satisfies the EventGenerator API required for the EventAccumulator.
    Satisfies the EventWriter API required to create a tf.summary.FileWriter.

    Has additional convenience methods for adding test events.
    """

    def __init__(self, testcase, zero_out_timestamps=False):
        self._testcase = testcase
        self.items = []
        self.zero_out_timestamps = zero_out_timestamps
        self._initial_metadata = {}

    def Load(self):
        while self.items:
            event = self.items.pop(0)
            event = data_compat.migrate_event(event)
            events = dataclass_compat.migrate_event(
                event, self._initial_metadata
            )
            for event in events:
                yield event

    def AddScalarTensor(self, tag, wall_time=0, step=0, value=0):
        """Add a rank-0 tensor event.

        Note: This is not related to the scalar plugin; it's just a
        convenience function to add an event whose contents aren't
        important.
        """
        tensor = tensor_util.make_tensor_proto(float(value))
        event = event_pb2.Event(
            wall_time=wall_time,
            step=step,
            summary=summary_pb2.Summary(
                value=[summary_pb2.Summary.Value(tag=tag, tensor=tensor)]
            ),
        )
        self.AddEvent(event)

    def AddEvent(self, event):
        event = event_pb2.Event.FromString(event.SerializeToString())
        if self.zero_out_timestamps:
            event.wall_time = 0.0
        self.items.append(event)

    def add_event(self, event):  # pylint: disable=invalid-name
        """Match the EventWriter API."""
        self.AddEvent(event)

    def get_logdir(self):  # pylint: disable=invalid-name
        """Return a temp directory for asset writing."""
        return self._testcase.get_temp_dir()


class EventAccumulatorTest(tf.test.TestCase):
    def assertTagsEqual(self, actual, expected):
        """Utility method for checking the return value of the Tags() call.

        It fills out the `expected` arg with the default (empty) values for every
        tag type, so that the author needs only specify the non-empty values they
        are interested in testing.

        Args:
          actual: The actual Accumulator tags response.
          expected: The expected tags response (empty fields may be omitted)
        """

        empty_tags = {
            ea.GRAPH: False,
            ea.META_GRAPH: False,
            ea.RUN_METADATA: [],
            ea.TENSORS: [],
        }

        # Verifies that there are no unexpected keys in the actual response.
        # If this line fails, likely you added a new tag type, and need to update
        # the empty_tags dictionary above.
        self.assertItemsEqual(actual.keys(), empty_tags.keys())

        for key in actual:
            expected_value = expected.get(key, empty_tags[key])
            if isinstance(expected_value, list):
                self.assertItemsEqual(actual[key], expected_value)
            else:
                self.assertEqual(actual[key], expected_value)


class MockingEventAccumulatorTest(EventAccumulatorTest):
    def setUp(self):
        super().setUp()
        self.stubs = tf.compat.v1.test.StubOutForTesting()

    def tearDown(self):
        super().tearDown()
        self.stubs.CleanUp()

    def _make_accumulator(self, generator, **kwargs):
        patcher = mock.patch.object(ea, "_GeneratorFromPath", autospec=True)
        mock_impl = patcher.start()
        mock_impl.return_value = generator
        self.addCleanup(patcher.stop)
        return ea.EventAccumulator("path/is/ignored", **kwargs)

    def testEmptyAccumulator(self):
        gen = _EventGenerator(self)
        x = self._make_accumulator(gen)
        x.Reload()
        self.assertTagsEqual(x.Tags(), {})

    def testReload(self):
        """EventAccumulator contains suitable tags after calling Reload."""
        gen = _EventGenerator(self)
        acc = self._make_accumulator(gen)
        acc.Reload()
        self.assertTagsEqual(acc.Tags(), {})
        gen.AddScalarTensor("s1", wall_time=1, step=10, value=50)
        gen.AddScalarTensor("s2", wall_time=1, step=10, value=80)
        acc.Reload()
        self.assertTagsEqual(
            acc.Tags(),
            {
                ea.TENSORS: ["s1", "s2"],
            },
        )

    def testKeyError(self):
        """KeyError should be raised when accessing non-existing keys."""
        gen = _EventGenerator(self)
        acc = self._make_accumulator(gen)
        acc.Reload()
        with self.assertRaises(KeyError):
            acc.Tensors("s1")

    def testNonValueEvents(self):
        """Non-value events in the generator don't cause early exits."""
        gen = _EventGenerator(self)
        acc = self._make_accumulator(gen)
        gen.AddScalarTensor("s1", wall_time=1, step=10, value=20)
        gen.AddEvent(
            event_pb2.Event(wall_time=2, step=20, file_version="nots2")
        )
        gen.AddScalarTensor("s3", wall_time=3, step=100, value=1)

        acc.Reload()
        self.assertTagsEqual(
            acc.Tags(),
            {
                ea.TENSORS: ["s1", "s3"],
            },
        )

    def testExpiredDataDiscardedAfterRestartForFileVersionLessThan2(self):
        """Tests that events are discarded after a restart is detected.

        If a step value is observed to be lower than what was previously seen,
        this should force a discard of all previous items with the same tag
        that are outdated.

        Only file versions < 2 use this out-of-order discard logic. Later versions
        discard events based on the step value of SessionLog.START.
        """
        warnings = []
        self.stubs.Set(logger, "warning", warnings.append)

        gen = _EventGenerator(self)
        acc = self._make_accumulator(gen)

        gen.AddEvent(
            event_pb2.Event(wall_time=0, step=0, file_version="brain.Event:1")
        )
        gen.AddScalarTensor("s1", wall_time=1, step=100, value=20)
        gen.AddScalarTensor("s1", wall_time=1, step=200, value=20)
        gen.AddScalarTensor("s1", wall_time=1, step=300, value=20)
        acc.Reload()
        ## Check that number of items are what they should be
        self.assertEqual([x.step for x in acc.Tensors("s1")], [100, 200, 300])

        gen.AddScalarTensor("s1", wall_time=1, step=101, value=20)
        gen.AddScalarTensor("s1", wall_time=1, step=201, value=20)
        gen.AddScalarTensor("s1", wall_time=1, step=301, value=20)
        acc.Reload()
        ## Check that we have discarded 200 and 300 from s1
        self.assertEqual(
            [x.step for x in acc.Tensors("s1")], [100, 101, 201, 301]
        )

    def testOrphanedDataNotDiscardedIfFlagUnset(self):
        """Tests that events are not discarded if purge_orphaned_data is
        false."""
        gen = _EventGenerator(self)
        acc = self._make_accumulator(gen, purge_orphaned_data=False)

        gen.AddEvent(
            event_pb2.Event(wall_time=0, step=0, file_version="brain.Event:1")
        )
        gen.AddScalarTensor("s1", wall_time=1, step=100, value=20)
        gen.AddScalarTensor("s1", wall_time=1, step=200, value=20)
        gen.AddScalarTensor("s1", wall_time=1, step=300, value=20)
        acc.Reload()
        ## Check that number of items are what they should be
        self.assertEqual([x.step for x in acc.Tensors("s1")], [100, 200, 300])

        gen.AddScalarTensor("s1", wall_time=1, step=101, value=20)
        gen.AddScalarTensor("s1", wall_time=1, step=201, value=20)
        gen.AddScalarTensor("s1", wall_time=1, step=301, value=20)
        acc.Reload()
        ## Check that we have NOT discarded 200 and 300 from s1
        self.assertEqual(
            [x.step for x in acc.Tensors("s1")], [100, 200, 300, 101, 201, 301]
        )

    def testEventsDiscardedPerTagAfterRestartForFileVersionLessThan2(self):
        """Tests that event discards after restart, only affect the misordered
        tag.

        If a step value is observed to be lower than what was previously seen,
        this should force a discard of all previous items that are outdated, but
        only for the out of order tag. Other tags should remain unaffected.

        Only file versions < 2 use this out-of-order discard logic. Later versions
        discard events based on the step value of SessionLog.START.
        """
        warnings = []
        self.stubs.Set(logger, "warning", warnings.append)

        gen = _EventGenerator(self)
        acc = self._make_accumulator(gen)

        gen.AddEvent(
            event_pb2.Event(wall_time=0, step=0, file_version="brain.Event:1")
        )
        gen.AddScalarTensor("s1", wall_time=1, step=100, value=20)
        gen.AddScalarTensor("s2", wall_time=1, step=101, value=20)
        gen.AddScalarTensor("s1", wall_time=1, step=200, value=20)
        gen.AddScalarTensor("s2", wall_time=1, step=201, value=20)
        gen.AddScalarTensor("s1", wall_time=1, step=300, value=20)
        gen.AddScalarTensor("s2", wall_time=1, step=301, value=20)
        gen.AddScalarTensor("s1", wall_time=1, step=101, value=20)
        gen.AddScalarTensor("s3", wall_time=1, step=101, value=20)
        gen.AddScalarTensor("s1", wall_time=1, step=201, value=20)
        gen.AddScalarTensor("s1", wall_time=1, step=301, value=20)

        acc.Reload()
        ## Check that we have discarded 200 and 300 for s1
        self.assertEqual(
            [x.step for x in acc.Tensors("s1")], [100, 101, 201, 301]
        )

        ## Check that s1 discards do not affect s2 (written before out-of-order)
        ## or s3 (written after out-of-order).
        ## i.e. check that only events from the out of order tag are discarded
        self.assertEqual([x.step for x in acc.Tensors("s2")], [101, 201, 301])
        self.assertEqual([x.step for x in acc.Tensors("s3")], [101])

    def testOnlySummaryEventsTriggerDiscards(self):
        """Test that file version event does not trigger data purge."""
        gen = _EventGenerator(self)
        acc = self._make_accumulator(gen)
        gen.AddScalarTensor("s1", wall_time=1, step=100, value=20)
        ev1 = event_pb2.Event(wall_time=2, step=0, file_version="brain.Event:1")
        graph_bytes = tf.compat.v1.GraphDef().SerializeToString()
        ev2 = event_pb2.Event(wall_time=3, step=0, graph_def=graph_bytes)
        gen.AddEvent(ev1)
        gen.AddEvent(ev2)
        acc.Reload()
        self.assertEqual([x.step for x in acc.Tensors("s1")], [100])

    def testSessionLogStartMessageDiscardsExpiredEvents(self):
        """Test that SessionLog.START message discards expired events.

        This discard logic is preferred over the out-of-order step
        discard logic, but this logic can only be used for event protos
        which have the SessionLog enum, which was introduced to
        event.proto for file_version >= brain.Event:2.
        """
        gen = _EventGenerator(self)
        acc = self._make_accumulator(gen)
        slog = event_pb2.SessionLog(status=event_pb2.SessionLog.START)

        gen.AddEvent(
            event_pb2.Event(wall_time=0, step=1, file_version="brain.Event:2")
        )

        gen.AddScalarTensor("s1", wall_time=1, step=100, value=20)
        gen.AddEvent(event_pb2.Event(wall_time=1, step=100, session_log=slog))
        gen.AddScalarTensor("s1", wall_time=1, step=200, value=20)
        gen.AddScalarTensor("s1", wall_time=1, step=300, value=20)
        gen.AddScalarTensor("s1", wall_time=1, step=400, value=20)

        gen.AddScalarTensor("s2", wall_time=1, step=202, value=20)
        gen.AddScalarTensor("s2", wall_time=1, step=203, value=20)

        gen.AddEvent(event_pb2.Event(wall_time=2, step=201, session_log=slog))
        acc.Reload()
        self.assertEqual([x.step for x in acc.Tensors("s1")], [100, 200])
        self.assertEqual([x.step for x in acc.Tensors("s2")], [])

    def testFirstEventTimestamp(self):
        """Test that FirstEventTimestamp() returns wall_time of the first
        event."""
        gen = _EventGenerator(self)
        acc = self._make_accumulator(gen)
        gen.AddEvent(
            event_pb2.Event(wall_time=10, step=20, file_version="brain.Event:2")
        )
        gen.AddScalarTensor("s1", wall_time=30, step=40, value=20)
        self.assertEqual(acc.FirstEventTimestamp(), 10)

    def testReloadPopulatesFirstEventTimestamp(self):
        """Test that Reload() means FirstEventTimestamp() won't load events."""
        gen = _EventGenerator(self)
        acc = self._make_accumulator(gen)
        gen.AddEvent(
            event_pb2.Event(wall_time=1, step=2, file_version="brain.Event:2")
        )

        acc.Reload()

        def _Die(*args, **kwargs):  # pylint: disable=unused-argument
            raise RuntimeError("Load() should not be called")

        self.stubs.Set(gen, "Load", _Die)
        self.assertEqual(acc.FirstEventTimestamp(), 1)

    def testFirstEventTimestampLoadsEvent(self):
        """Test that FirstEventTimestamp() doesn't discard the loaded event."""
        gen = _EventGenerator(self)
        acc = self._make_accumulator(gen)
        gen.AddEvent(
            event_pb2.Event(wall_time=1, step=2, file_version="brain.Event:2")
        )

        self.assertEqual(acc.FirstEventTimestamp(), 1)
        acc.Reload()
        self.assertEqual(acc.file_version, 2.0)

    def testGetSourceWriter(self):
        gen = _EventGenerator(self)
        acc = self._make_accumulator(gen)
        gen.AddEvent(
            event_pb2.Event(
                wall_time=10,
                step=20,
                source_metadata=event_pb2.SourceMetadata(
                    writer="custom_writer"
                ),
            )
        )
        gen.AddScalarTensor("s1", wall_time=30, step=40, value=20)
        self.assertEqual(acc.GetSourceWriter(), "custom_writer")

    def testReloadPopulatesSourceWriter(self):
        """Test that Reload() means GetSourceWriter() won't load events."""
        gen = _EventGenerator(self)
        acc = self._make_accumulator(gen)
        gen.AddEvent(
            event_pb2.Event(
                wall_time=1,
                step=2,
                source_metadata=event_pb2.SourceMetadata(
                    writer="custom_writer"
                ),
            )
        )
        acc.Reload()

        def _Die(*args, **kwargs):  # pylint: disable=unused-argument
            raise RuntimeError("Load() should not be called")

        self.stubs.Set(gen, "Load", _Die)
        self.assertEqual(acc.GetSourceWriter(), "custom_writer")

    def testGetSourceWriterLoadsEvent(self):
        """Test that GetSourceWriter() doesn't discard the loaded event."""
        gen = _EventGenerator(self)
        acc = self._make_accumulator(gen)
        gen.AddEvent(
            event_pb2.Event(
                wall_time=1,
                step=2,
                file_version="brain.Event:2",
                source_metadata=event_pb2.SourceMetadata(
                    writer="custom_writer"
                ),
            )
        )
        self.assertEqual(acc.GetSourceWriter(), "custom_writer")
        acc.Reload()
        self.assertEqual(acc.file_version, 2.0)

    def testNewStyleScalarSummary(self):
        """Verify processing of tensorboard.plugins.scalar.summary."""
        event_sink = _EventGenerator(self, zero_out_timestamps=True)
        writer = test_util.FileWriter(self.get_temp_dir())
        writer.event_writer = event_sink
        with tf.compat.v1.Graph().as_default():
            with self.test_session() as sess:
                step = tf.compat.v1.placeholder(tf.float32, shape=[])
                scalar_summary.op(
                    "accuracy", 1.0 - 1.0 / (step + tf.constant(1.0))
                )
                scalar_summary.op("xent", 1.0 / (step + tf.constant(1.0)))
                merged = tf.compat.v1.summary.merge_all()
                writer.add_graph(sess.graph)
                for i in range(10):
                    summ = sess.run(merged, feed_dict={step: float(i)})
                    writer.add_summary(summ, global_step=i)

        accumulator = self._make_accumulator(event_sink)
        accumulator.Reload()

        tags = [
            graph_metadata.RUN_GRAPH_NAME,
            "accuracy/scalar_summary",
            "xent/scalar_summary",
        ]
        self.assertTagsEqual(
            accumulator.Tags(),
            {
                ea.TENSORS: tags,
                ea.GRAPH: True,
                ea.META_GRAPH: False,
            },
        )

        self.assertItemsEqual(
            accumulator.ActivePlugins(),
            [scalar_metadata.PLUGIN_NAME, graph_metadata.PLUGIN_NAME],
        )

    def testNewStyleAudioSummary(self):
        """Verify processing of tensorboard.plugins.audio.summary."""
        event_sink = _EventGenerator(self, zero_out_timestamps=True)
        writer = test_util.FileWriter(self.get_temp_dir())
        writer.event_writer = event_sink
        with tf.compat.v1.Graph().as_default():
            with self.test_session() as sess:
                ipt = tf.random.normal(shape=[5, 441, 2])
                with tf.name_scope("1"):
                    audio_summary.op(
                        "one", ipt, sample_rate=44100, max_outputs=1
                    )
                with tf.name_scope("2"):
                    audio_summary.op(
                        "two", ipt, sample_rate=44100, max_outputs=2
                    )
                with tf.name_scope("3"):
                    audio_summary.op(
                        "three", ipt, sample_rate=44100, max_outputs=3
                    )
                merged = tf.compat.v1.summary.merge_all()
                writer.add_graph(sess.graph)
                for i in range(10):
                    summ = sess.run(merged)
                    writer.add_summary(summ, global_step=i)

        accumulator = self._make_accumulator(event_sink)
        accumulator.Reload()

        tags = [
            graph_metadata.RUN_GRAPH_NAME,
            "1/one/audio_summary",
            "2/two/audio_summary",
            "3/three/audio_summary",
        ]
        self.assertTagsEqual(
            accumulator.Tags(),
            {
                ea.TENSORS: tags,
                ea.GRAPH: True,
                ea.META_GRAPH: False,
            },
        )

        self.assertItemsEqual(
            accumulator.ActivePlugins(),
            [audio_metadata.PLUGIN_NAME, graph_metadata.PLUGIN_NAME],
        )

    def testNewStyleImageSummary(self):
        """Verify processing of tensorboard.plugins.image.summary."""
        event_sink = _EventGenerator(self, zero_out_timestamps=True)
        writer = test_util.FileWriter(self.get_temp_dir())
        writer.event_writer = event_sink
        with tf.compat.v1.Graph().as_default():
            with self.test_session() as sess:
                ipt = tf.ones([10, 4, 4, 3], tf.uint8)
                # This is an interesting example, because the old tf.image_summary op
                # would throw an error here, because it would be tag reuse.
                # Using the tf node name instead allows argument re-use to the image
                # summary.
                with tf.name_scope("1"):
                    image_summary.op("images", ipt, max_outputs=1)
                with tf.name_scope("2"):
                    image_summary.op("images", ipt, max_outputs=2)
                with tf.name_scope("3"):
                    image_summary.op("images", ipt, max_outputs=3)
                merged = tf.compat.v1.summary.merge_all()
                writer.add_graph(sess.graph)
                for i in range(10):
                    summ = sess.run(merged)
                    writer.add_summary(summ, global_step=i)

        accumulator = self._make_accumulator(event_sink)
        accumulator.Reload()

        tags = [
            graph_metadata.RUN_GRAPH_NAME,
            "1/images/image_summary",
            "2/images/image_summary",
            "3/images/image_summary",
        ]
        self.assertTagsEqual(
            accumulator.Tags(),
            {
                ea.TENSORS: tags,
                ea.GRAPH: True,
                ea.META_GRAPH: False,
            },
        )

        self.assertItemsEqual(
            accumulator.ActivePlugins(),
            [image_metadata.PLUGIN_NAME, graph_metadata.PLUGIN_NAME],
        )

    def testTFSummaryTensor(self):
        """Verify processing of tf.summary.tensor."""
        event_sink = _EventGenerator(self, zero_out_timestamps=True)
        writer = test_util.FileWriter(self.get_temp_dir())
        writer.event_writer = event_sink
        with tf.compat.v1.Graph().as_default():
            with self.test_session() as sess:
                tensor_summary = tf.compat.v1.summary.tensor_summary
                tensor_summary("scalar", tf.constant(1.0))
                tensor_summary("vector", tf.constant([1.0, 2.0, 3.0]))
                tensor_summary("string", tf.constant(b"foobar"))
                merged = tf.compat.v1.summary.merge_all()
                summ = sess.run(merged)
                writer.add_summary(summ, 0)

        accumulator = self._make_accumulator(event_sink)
        accumulator.Reload()

        self.assertTagsEqual(
            accumulator.Tags(),
            {
                ea.TENSORS: ["scalar", "vector", "string"],
            },
        )

        scalar_proto = accumulator.Tensors("scalar")[0].tensor_proto
        scalar = tensor_util.make_ndarray(scalar_proto)
        vector_proto = accumulator.Tensors("vector")[0].tensor_proto
        vector = tensor_util.make_ndarray(vector_proto)
        string_proto = accumulator.Tensors("string")[0].tensor_proto
        string = tensor_util.make_ndarray(string_proto)

        self.assertTrue(np.array_equal(scalar, 1.0))
        self.assertTrue(np.array_equal(vector, [1.0, 2.0, 3.0]))
        self.assertTrue(np.array_equal(string, b"foobar"))

        self.assertItemsEqual(accumulator.ActivePlugins(), [])

    def _testTFSummaryTensor_SizeGuidance(
        self, plugin_name, tensor_size_guidance, steps, expected_count
    ):
        event_sink = _EventGenerator(self, zero_out_timestamps=True)
        writer = test_util.FileWriter(self.get_temp_dir())
        writer.event_writer = event_sink
        with tf.compat.v1.Graph().as_default():
            with self.test_session() as sess:
                summary_metadata = summary_pb2.SummaryMetadata(
                    plugin_data=summary_pb2.SummaryMetadata.PluginData(
                        plugin_name=plugin_name, content=b"{}"
                    )
                )
                tf.compat.v1.summary.tensor_summary(
                    "scalar",
                    tf.constant(1.0),
                    summary_metadata=summary_metadata,
                )
                merged = tf.compat.v1.summary.merge_all()
                for step in range(steps):
                    writer.add_summary(sess.run(merged), global_step=step)

        accumulator = self._make_accumulator(
            event_sink, tensor_size_guidance=tensor_size_guidance
        )
        accumulator.Reload()

        tensors = accumulator.Tensors("scalar")
        self.assertEqual(len(tensors), expected_count)

    def testTFSummaryTensor_SizeGuidance_DefaultToTensorGuidance(self):
        self._testTFSummaryTensor_SizeGuidance(
            plugin_name="jabberwocky",
            tensor_size_guidance={},
            steps=ea.DEFAULT_SIZE_GUIDANCE[ea.TENSORS] + 1,
            expected_count=ea.DEFAULT_SIZE_GUIDANCE[ea.TENSORS],
        )

    def testTFSummaryTensor_SizeGuidance_UseSmallSingularPluginGuidance(self):
        size = int(ea.DEFAULT_SIZE_GUIDANCE[ea.TENSORS] / 2)
        assert size < ea.DEFAULT_SIZE_GUIDANCE[ea.TENSORS], size
        self._testTFSummaryTensor_SizeGuidance(
            plugin_name="jabberwocky",
            tensor_size_guidance={"jabberwocky": size},
            steps=ea.DEFAULT_SIZE_GUIDANCE[ea.TENSORS] + 1,
            expected_count=size,
        )

    def testTFSummaryTensor_SizeGuidance_UseLargeSingularPluginGuidance(self):
        size = ea.DEFAULT_SIZE_GUIDANCE[ea.TENSORS] + 5
        self._testTFSummaryTensor_SizeGuidance(
            plugin_name="jabberwocky",
            tensor_size_guidance={"jabberwocky": size},
            steps=ea.DEFAULT_SIZE_GUIDANCE[ea.TENSORS] + 10,
            expected_count=size,
        )

    def testTFSummaryTensor_SizeGuidance_IgnoreIrrelevantGuidances(self):
        size_small = int(ea.DEFAULT_SIZE_GUIDANCE[ea.TENSORS] / 3)
        size_large = int(ea.DEFAULT_SIZE_GUIDANCE[ea.TENSORS] / 2)
        assert size_small < size_large < ea.DEFAULT_SIZE_GUIDANCE[ea.TENSORS], (
            size_small,
            size_large,
        )
        self._testTFSummaryTensor_SizeGuidance(
            plugin_name="jabberwocky",
            tensor_size_guidance={
                "jabberwocky": size_small,
                "wnoorejbpxl": size_large,
            },
            steps=ea.DEFAULT_SIZE_GUIDANCE[ea.TENSORS] + 1,
            expected_count=size_small,
        )


class RealisticEventAccumulatorTest(EventAccumulatorTest):
    def testTensorsRealistically(self):
        """Test accumulator by writing values and then reading them."""

        def FakeScalarSummary(tag, value):
            value = summary_pb2.Summary.Value(tag=tag, simple_value=value)
            summary = summary_pb2.Summary(value=[value])
            return summary

        directory = os.path.join(self.get_temp_dir(), "values_dir")
        if tf.io.gfile.isdir(directory):
            tf.io.gfile.rmtree(directory)
        tf.io.gfile.mkdir(directory)

        writer = test_util.FileWriter(directory, max_queue=100)

        with tf.Graph().as_default() as graph:
            _ = tf.constant([2.0, 1.0])
            # Add a graph to the summary writer.
            writer.add_graph(graph)
            graph_def = graph.as_graph_def(add_shapes=True)
            meta_graph_def = tf.compat.v1.train.export_meta_graph(
                graph_def=graph_def
            )
            writer.add_meta_graph(meta_graph_def)

        run_metadata = config_pb2.RunMetadata()
        device_stats = run_metadata.step_stats.dev_stats.add()
        device_stats.device = "test device"
        writer.add_run_metadata(run_metadata, "test run")

        # Write a bunch of events using the writer.
        for i in range(30):
            summ_id = FakeScalarSummary("id", i)
            summ_sq = FakeScalarSummary("sq", i * i)
            writer.add_summary(summ_id, i * 5)
            writer.add_summary(summ_sq, i * 5)
        writer.flush()

        # Verify that we can load those events properly
        acc = ea.EventAccumulator(directory)
        acc.Reload()
        self.assertTagsEqual(
            acc.Tags(),
            {
                ea.TENSORS: [
                    graph_metadata.RUN_GRAPH_NAME,
                    "id",
                    "sq",
                    "test run",
                ],
                ea.GRAPH: True,
                ea.META_GRAPH: True,
                ea.RUN_METADATA: [],
            },
        )
        id_events = acc.Tensors("id")
        sq_events = acc.Tensors("sq")
        self.assertEqual(30, len(id_events))
        self.assertEqual(30, len(sq_events))
        for i in range(30):
            self.assertEqual(i * 5, id_events[i].step)
            self.assertEqual(i * 5, sq_events[i].step)
            self.assertEqual(
                i, tensor_util.make_ndarray(id_events[i].tensor_proto).item()
            )
            self.assertEqual(
                i * i,
                tensor_util.make_ndarray(sq_events[i].tensor_proto).item(),
            )

        # Write a few more events to test incremental reloading
        for i in range(30, 40):
            summ_id = FakeScalarSummary("id", i)
            summ_sq = FakeScalarSummary("sq", i * i)
            writer.add_summary(summ_id, i * 5)
            writer.add_summary(summ_sq, i * 5)
        writer.flush()

        # Verify we can now see all of the data
        acc.Reload()
        id_events = acc.Tensors("id")
        sq_events = acc.Tensors("sq")
        self.assertEqual(40, len(id_events))
        self.assertEqual(40, len(sq_events))
        for i in range(40):
            self.assertEqual(i * 5, id_events[i].step)
            self.assertEqual(i * 5, sq_events[i].step)
            self.assertEqual(
                i, tensor_util.make_ndarray(id_events[i].tensor_proto).item()
            )
            self.assertEqual(
                i * i,
                tensor_util.make_ndarray(sq_events[i].tensor_proto).item(),
            )

        expected_graph_def = graph_pb2.GraphDef.FromString(
            graph.as_graph_def(add_shapes=True).SerializeToString()
        )
        self.assertProtoEquals(expected_graph_def, acc.Graph())
        self.assertProtoEquals(
            expected_graph_def,
            graph_pb2.GraphDef.FromString(acc.SerializedGraph()),
        )

        expected_meta_graph = meta_graph_pb2.MetaGraphDef.FromString(
            meta_graph_def.SerializeToString()
        )
        self.assertProtoEquals(expected_meta_graph, acc.MetaGraph())

    def testGraphFromMetaGraphBecomesAvailable(self):
        """Test accumulator by writing values and then reading them."""

        directory = os.path.join(
            self.get_temp_dir(), "metagraph_test_values_dir"
        )
        if tf.io.gfile.isdir(directory):
            tf.io.gfile.rmtree(directory)
        tf.io.gfile.mkdir(directory)

        writer = test_util.FileWriter(directory, max_queue=100)

        with tf.Graph().as_default() as graph:
            _ = tf.constant([2.0, 1.0])
            # Add a graph to the summary writer.
            graph_def = graph.as_graph_def(add_shapes=True)
            meta_graph_def = tf.compat.v1.train.export_meta_graph(
                graph_def=graph_def
            )
            writer.add_meta_graph(meta_graph_def)
            writer.flush()

        # Verify that we can load those events properly
        acc = ea.EventAccumulator(directory)
        acc.Reload()
        self.assertTagsEqual(
            acc.Tags(),
            {
                ea.GRAPH: True,
                ea.META_GRAPH: True,
            },
        )

        expected_graph_def = graph_pb2.GraphDef.FromString(
            graph.as_graph_def(add_shapes=True).SerializeToString()
        )
        self.assertProtoEquals(expected_graph_def, acc.Graph())
        self.assertProtoEquals(
            expected_graph_def,
            graph_pb2.GraphDef.FromString(acc.SerializedGraph()),
        )

        expected_meta_graph = meta_graph_pb2.MetaGraphDef.FromString(
            meta_graph_def.SerializeToString()
        )
        self.assertProtoEquals(expected_meta_graph, acc.MetaGraph())

    def _writeMetadata(self, logdir, summary_metadata, nonce=""):
        """Write to disk a summary with the given metadata.

        Arguments:
          logdir: a string
          summary_metadata: a `SummaryMetadata` protobuf object
          nonce: optional; will be added to the end of the event file name
            to guarantee that multiple calls to this function do not stomp the
            same file
        """

        summary = summary_pb2.Summary()
        summary.value.add(
            tensor=tensor_util.make_tensor_proto(
                ["po", "ta", "to"], dtype=tf.string
            ),
            tag="you_are_it",
            metadata=summary_metadata,
        )
        writer = test_util.FileWriter(logdir, filename_suffix=nonce)
        writer.add_summary(summary.SerializeToString())
        writer.close()

    def testSummaryMetadata(self):
        logdir = self.get_temp_dir()
        summary_metadata = summary_pb2.SummaryMetadata(
            display_name="current tagee",
            summary_description="no",
            plugin_data=summary_pb2.SummaryMetadata.PluginData(
                plugin_name="outlet"
            ),
        )
        self._writeMetadata(logdir, summary_metadata)
        acc = ea.EventAccumulator(logdir)
        acc.Reload()
        self.assertProtoEquals(
            summary_metadata, acc.SummaryMetadata("you_are_it")
        )

    def testSummaryMetadata_FirstMetadataWins(self):
        logdir = self.get_temp_dir()
        summary_metadata_1 = summary_pb2.SummaryMetadata(
            display_name="current tagee",
            summary_description="no",
            plugin_data=summary_pb2.SummaryMetadata.PluginData(
                plugin_name="outlet", content=b"120v"
            ),
        )
        self._writeMetadata(logdir, summary_metadata_1, nonce="1")
        acc = ea.EventAccumulator(logdir)
        acc.Reload()
        summary_metadata_2 = summary_pb2.SummaryMetadata(
            display_name="tagee of the future",
            summary_description="definitely not",
            plugin_data=summary_pb2.SummaryMetadata.PluginData(
                plugin_name="plug", content=b"110v"
            ),
        )
        self._writeMetadata(logdir, summary_metadata_2, nonce="2")
        acc.Reload()

        self.assertProtoEquals(
            summary_metadata_1, acc.SummaryMetadata("you_are_it")
        )

    def testPluginTagToContent_PluginsCannotJumpOnTheBandwagon(self):
        # If there are multiple `SummaryMetadata` for a given tag, and the
        # set of plugins in the `plugin_data` of second is different from
        # that of the first, then the second set should be ignored.
        logdir = self.get_temp_dir()
        summary_metadata_1 = summary_pb2.SummaryMetadata(
            display_name="current tagee",
            summary_description="no",
            plugin_data=summary_pb2.SummaryMetadata.PluginData(
                plugin_name="outlet", content=b"120v"
            ),
        )
        self._writeMetadata(logdir, summary_metadata_1, nonce="1")
        acc = ea.EventAccumulator(logdir)
        acc.Reload()
        summary_metadata_2 = summary_pb2.SummaryMetadata(
            display_name="tagee of the future",
            summary_description="definitely not",
            plugin_data=summary_pb2.SummaryMetadata.PluginData(
                plugin_name="plug", content=b"110v"
            ),
        )
        self._writeMetadata(logdir, summary_metadata_2, nonce="2")
        acc.Reload()

        self.assertEqual(
            acc.PluginTagToContent("outlet"), {"you_are_it": b"120v"}
        )
        with self.assertRaisesRegex(KeyError, "plug"):
            acc.PluginTagToContent("plug")
        self.assertItemsEqual(acc.ActivePlugins(), ["outlet"])


if __name__ == "__main__":
    tf.test.main()
