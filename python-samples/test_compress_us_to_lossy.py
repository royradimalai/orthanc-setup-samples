import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest


class FakeDicom:
    def __init__(self, tags, frames=10, transferSyntax='1.2.840.10008.1.2.1'):
        self.tags = tags
        self.frames = frames
        self.transferSyntax = transferSyntax

    def GetInstanceSimplifiedJson(self):
        return json.dumps(self.tags)

    def GetInstanceFramesCount(self):
        return self.frames

    def GetInstanceTransferSyntaxUid(self):
        return self.transferSyntax

    def GetInstanceSize(self):
        return 1000


class FakeOrthanc(types.ModuleType):
    def __init__(self):
        super().__init__('orthanc')
        self.configuration = {'OverwriteInstances': True}
        self.calls = []
        self.errors = []
        self.infos = []
        self.callback = None
        self.uploadId = 'source-id'
        self.transcoded = None

    def GetConfiguration(self):
        return json.dumps(self.configuration)

    def RegisterOnStoredInstanceCallback(self, callback):
        self.callback = callback

    def RestApiPost(self, path, body):
        self.calls.append((path, body))
        if path == '/instances':
            return json.dumps({'ID': self.uploadId}).encode()
        return b'transcoded'

    def CreateDicomInstance(self, value):
        return self.transcoded

    def LogError(self, message):
        self.errors.append(message)

    def LogInfo(self, message):
        self.infos.append(message)


ORTHANC = FakeOrthanc()
sys.modules['orthanc'] = ORTHANC
SCRIPT = Path(__file__).with_name('compress_us_to_lossy.py')
SPEC = importlib.util.spec_from_file_location('compress_us_to_lossy', SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def MakeTags(**overrides):
    tags = {
        'Modality': 'US',
        'PatientID': 'patient',
        'StudyInstanceUID': 'study',
        'SeriesInstanceUID': 'series',
        'SOPInstanceUID': 'sop',
        'ImageType': 'ORIGINAL\\PRIMARY',
    }
    tags.update(overrides)
    return tags


class CompressUltrasoundTests(unittest.TestCase):
    def setUp(self):
        ORTHANC.configuration = {'OverwriteInstances': True}
        ORTHANC.calls = []
        ORTHANC.errors = []
        ORTHANC.infos = []
        ORTHANC.uploadId = 'source-id'
        ORTHANC.transcoded = FakeDicom(
            MakeTags(
                ImageType='DERIVED\\PRIMARY',
                LossyImageCompression='01',
                LossyImageCompressionMethod='ISO_10918_1',
            ),
            transferSyntax=MODULE.TARGET_TRANSFER_SYNTAX,
        )

    def test_replaces_multiframe_ultrasound_with_validated_lossy_copy(self):
        source = FakeDicom(MakeTags())

        MODULE.OnStoredInstance(source, 'source-id')

        self.assertEqual([call[0] for call in ORTHANC.calls], [
            '/instances/source-id/modify',
            '/instances',
        ])
        modification = json.loads(ORTHANC.calls[0][1])
        self.assertEqual(modification['Replace']['SOPInstanceUID'], 'sop')
        self.assertEqual(modification['Replace']['LossyImageCompression'], '01')
        self.assertEqual(
            modification['Replace']['LossyImageCompressionMethod'],
            'ISO_10918_1',
        )
        self.assertEqual(modification['Replace']['ImageType'], 'DERIVED\\PRIMARY')
        self.assertTrue(ORTHANC.infos)

    def test_skips_non_ultrasound(self):
        MODULE.OnStoredInstance(FakeDicom(MakeTags(Modality='DX')), 'source-id')
        self.assertEqual(ORTHANC.calls, [])

    def test_skips_single_frame_ultrasound(self):
        MODULE.OnStoredInstance(FakeDicom(MakeTags(), frames=1), 'source-id')
        self.assertEqual(ORTHANC.calls, [])

    def test_skips_already_lossy_ultrasound(self):
        source = FakeDicom(
            MakeTags(LossyImageCompression='01'),
            transferSyntax=MODULE.TARGET_TRANSFER_SYNTAX,
        )
        MODULE.OnStoredInstance(source, 'source-id')
        self.assertEqual(ORTHANC.calls, [])

    def test_keeps_original_when_identity_changes(self):
        ORTHANC.transcoded.tags['SOPInstanceUID'] = 'different'

        MODULE.OnStoredInstance(FakeDicom(MakeTags()), 'source-id')

        self.assertEqual([call[0] for call in ORTHANC.calls], [
            '/instances/source-id/modify',
        ])
        self.assertIn('SOPInstanceUID changed', ORTHANC.errors[0])

    def test_keeps_original_when_frame_count_changes(self):
        ORTHANC.transcoded.frames = 9

        MODULE.OnStoredInstance(FakeDicom(MakeTags()), 'source-id')

        self.assertEqual(len(ORTHANC.calls), 1)
        self.assertIn('frame count changed', ORTHANC.errors[0])

    def test_keeps_original_when_lossy_metadata_is_missing(self):
        del ORTHANC.transcoded.tags['LossyImageCompression']

        MODULE.OnStoredInstance(FakeDicom(MakeTags()), 'source-id')

        self.assertEqual(len(ORTHANC.calls), 1)
        self.assertIn('lossy compression metadata is missing', ORTHANC.errors[0])

    def test_requires_overwrite_configuration(self):
        ORTHANC.configuration = {'OverwriteInstances': False}
        with self.assertRaisesRegex(RuntimeError, 'OverwriteInstances'):
            MODULE.ValidateConfiguration()

    def test_accepts_new_overwrite_configuration_values(self):
        for value in ('Always', 'IfChanged'):
            with self.subTest(value=value):
                ORTHANC.configuration = {'OverwriteInstances': value}
                MODULE.ValidateConfiguration()

    def test_rejects_retranscoding_compressed_instances(self):
        ORTHANC.configuration = {
            'OverwriteInstances': True,
            'IngestTranscoding': '1.2.840.10008.1.2.4.70',
        }
        with self.assertRaisesRegex(
            RuntimeError,
            'IngestTranscodingOfCompressed',
        ):
            MODULE.ValidateConfiguration()


if __name__ == '__main__':
    unittest.main()
