import importlib.util
from io import BytesIO
import json
from pathlib import Path
import sys
import types
import unittest

from pydicom import dcmread, dcmwrite
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.encaps import encapsulate
from pydicom.uid import UID


JPEG_BASELINE = '1.2.840.10008.1.2.4.50'
EXPLICIT_VR_LITTLE_ENDIAN = '1.2.840.10008.1.2.1'
SOP_CLASS_UID = '1.2.840.10008.5.1.4.1.1.6.1'
STUDY_INSTANCE_UID = '1.2.826.0.1.3680043.8.498.1'
SERIES_INSTANCE_UID = '1.2.826.0.1.3680043.8.498.2'
SOURCE_SOP_INSTANCE_UID = '1.2.826.0.1.3680043.8.498.3'
GENERATED_SOP_INSTANCE_UID = '1.2.826.0.1.3680043.8.498.4'


def MakeTags(**overrides):
    tags = {
        'Modality': 'US',
        'PatientID': 'patient',
        'StudyInstanceUID': STUDY_INSTANCE_UID,
        'SeriesInstanceUID': SERIES_INSTANCE_UID,
        'SOPInstanceUID': SOURCE_SOP_INSTANCE_UID,
        'SOPClassUID': SOP_CLASS_UID,
        'ImageType': 'ORIGINAL\\PRIMARY',
    }
    tags.update(overrides)
    return tags


def MakeDicomBytes(
    sopInstanceUid=GENERATED_SOP_INSTANCE_UID,
    pixelData=b'jpeg-data',
):
    fileMeta = FileMetaDataset()
    fileMeta.MediaStorageSOPClassUID = SOP_CLASS_UID
    fileMeta.MediaStorageSOPInstanceUID = sopInstanceUid
    fileMeta.TransferSyntaxUID = JPEG_BASELINE

    dataset = FileDataset(None, {}, file_meta=fileMeta, preamble=b'\0' * 128)
    dataset.is_little_endian = True
    dataset.is_implicit_VR = False
    dataset.SOPClassUID = SOP_CLASS_UID
    dataset.SOPInstanceUID = sopInstanceUid
    dataset.PatientID = 'patient'
    dataset.StudyInstanceUID = STUDY_INSTANCE_UID
    dataset.SeriesInstanceUID = SERIES_INSTANCE_UID
    dataset.Modality = 'US'
    dataset.ImageType = ['DERIVED', 'PRIMARY']
    dataset.NumberOfFrames = 10
    dataset.LossyImageCompression = '01'
    dataset.LossyImageCompressionMethod = 'ISO_10918_1'
    dataset.PixelData = encapsulate([pixelData])
    dataset['PixelData'].is_undefined_length = True

    output = BytesIO()
    dcmwrite(output, dataset, write_like_original=False)
    return output.getvalue()


class FakeDicom:
    def __init__(
        self,
        tags,
        frames=10,
        transferSyntax=EXPLICIT_VR_LITTLE_ENDIAN,
        data=b'original',
    ):
        self.tags = tags
        self.frames = frames
        self.transferSyntax = transferSyntax
        self.data = data

    def GetInstanceSimplifiedJson(self):
        return json.dumps(self.tags)

    def GetInstanceFramesCount(self):
        return self.frames

    def GetInstanceTransferSyntaxUid(self):
        return self.transferSyntax

    def SerializeDicomInstance(self):
        return self.data


class FakeReceivedInstanceAction:
    KEEP_AS_IS = 1
    MODIFY = 2


class FakeOrthanc(types.ModuleType):
    def __init__(self):
        super().__init__('orthanc')
        self.ReceivedInstanceAction = FakeReceivedInstanceAction
        self.configuration = {'DicomLossyTranscodingQuality': 70}
        self.source = None
        self.transcoded = None
        self.transcodeError = None
        self.transcodeCalls = []
        self.errors = []
        self.infos = []
        self.callback = None

    def GetConfiguration(self):
        return json.dumps(self.configuration)

    def RegisterReceivedInstanceCallback(self, callback):
        self.callback = callback

    def CreateDicomInstance(self, value):
        if value == b'original':
            return self.source

        dataset = dcmread(BytesIO(value))
        tags = MakeTags(
            SOPInstanceUID=str(dataset.SOPInstanceUID),
            ImageType='\\'.join(dataset.ImageType),
        )
        if hasattr(dataset, 'LossyImageCompression'):
            tags['LossyImageCompression'] = str(dataset.LossyImageCompression)
        if hasattr(dataset, 'LossyImageCompressionMethod'):
            tags['LossyImageCompressionMethod'] = str(
                dataset.LossyImageCompressionMethod
            )
        return FakeDicom(
            tags,
            frames=int(dataset.NumberOfFrames),
            transferSyntax=str(dataset.file_meta.TransferSyntaxUID),
            data=value,
        )

    def TranscodeDicomInstance(self, received, transferSyntax):
        self.transcodeCalls.append((received, transferSyntax))
        if self.transcodeError:
            raise self.transcodeError
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


class CompressUltrasoundTests(unittest.TestCase):
    def setUp(self):
        ORTHANC.configuration = {'DicomLossyTranscodingQuality': 70}
        ORTHANC.source = FakeDicom(MakeTags())
        ORTHANC.transcoded = FakeDicom(
            MakeTags(
                SOPInstanceUID=GENERATED_SOP_INSTANCE_UID,
                ImageType='DERIVED\\PRIMARY',
                LossyImageCompression='01',
                LossyImageCompressionMethod='ISO_10918_1',
            ),
            transferSyntax=JPEG_BASELINE,
            data=MakeDicomBytes(),
        )
        ORTHANC.transcodeError = None
        ORTHANC.transcodeCalls = []
        ORTHANC.errors = []
        ORTHANC.infos = []

    def test_modifies_multiframe_ultrasound_before_storage(self):
        action, data = MODULE.ReceivedInstanceCallback(b'original', None)

        self.assertEqual(FakeReceivedInstanceAction.MODIFY, action)
        self.assertEqual([(b'original', JPEG_BASELINE)], ORTHANC.transcodeCalls)
        dataset = dcmread(BytesIO(data))
        expectedUid = MODULE.BuildDeterministicSopInstanceUid(
            SOURCE_SOP_INSTANCE_UID
        )
        self.assertEqual(expectedUid, dataset.SOPInstanceUID)
        self.assertEqual(
            expectedUid,
            dataset.file_meta.MediaStorageSOPInstanceUID,
        )
        self.assertTrue(ORTHANC.infos)

    def test_retransmission_uses_the_same_sop_instance_uid(self):
        firstAction, firstData = MODULE.ReceivedInstanceCallback(
            b'original',
            None,
        )
        ORTHANC.transcoded.data = MakeDicomBytes(
            '1.2.826.0.1.3680043.8.498.5'
        )
        secondAction, secondData = MODULE.ReceivedInstanceCallback(
            b'original',
            None,
        )

        self.assertEqual(firstAction, secondAction)
        self.assertEqual(firstData, secondData)

    def test_uid_rewrite_does_not_change_compressed_pixel_data(self):
        sourceBytes = MakeDicomBytes(pixelData=b'unchanged-pixels')

        rewrittenBytes, expectedUid = MODULE.RewriteSopInstanceUid(
            sourceBytes,
            SOURCE_SOP_INSTANCE_UID,
        )

        source = dcmread(BytesIO(sourceBytes))
        rewritten = dcmread(BytesIO(rewrittenBytes))
        self.assertEqual(source.PixelData, rewritten.PixelData)
        self.assertEqual(expectedUid, rewritten.SOPInstanceUID)
        self.assertEqual(
            expectedUid,
            rewritten.file_meta.MediaStorageSOPInstanceUID,
        )

    def test_compression_profile_change_generates_a_new_uid(self):
        originalVersion = MODULE.COMPRESSION_PROFILE_VERSION
        firstUid = MODULE.BuildDeterministicSopInstanceUid(
            SOURCE_SOP_INSTANCE_UID
        )
        try:
            MODULE.COMPRESSION_PROFILE_VERSION = originalVersion + 1
            secondUid = MODULE.BuildDeterministicSopInstanceUid(
                SOURCE_SOP_INSTANCE_UID
            )
        finally:
            MODULE.COMPRESSION_PROFILE_VERSION = originalVersion

        self.assertNotEqual(firstUid, secondUid)

    def test_deterministic_sop_instance_uid_is_valid(self):
        uid = MODULE.BuildDeterministicSopInstanceUid(
            SOURCE_SOP_INSTANCE_UID
        )

        self.assertTrue(UID(uid).is_valid)
        self.assertLessEqual(len(uid), 64)

    def test_accepts_list_or_string_derived_image_type(self):
        self.assertTrue(MODULE.IsDerived({'ImageType': ['DERIVED', 'PRIMARY']}))
        self.assertTrue(MODULE.IsDerived({'ImageType': 'DERIVED\\PRIMARY'}))
        self.assertFalse(MODULE.IsDerived({'ImageType': ['ORIGINAL', 'PRIMARY']}))

    def test_skips_non_ultrasound(self):
        ORTHANC.source.tags['Modality'] = 'DX'

        result = MODULE.ReceivedInstanceCallback(b'original', None)

        self.assertEqual((FakeReceivedInstanceAction.KEEP_AS_IS, None), result)
        self.assertEqual([], ORTHANC.transcodeCalls)

    def test_skips_single_frame_ultrasound(self):
        ORTHANC.source.frames = 1

        result = MODULE.ReceivedInstanceCallback(b'original', None)

        self.assertEqual((FakeReceivedInstanceAction.KEEP_AS_IS, None), result)
        self.assertEqual([], ORTHANC.transcodeCalls)

    def test_skips_already_lossy_ultrasound(self):
        ORTHANC.source.tags['LossyImageCompression'] = '01'
        ORTHANC.source.transferSyntax = JPEG_BASELINE

        result = MODULE.ReceivedInstanceCallback(b'original', None)

        self.assertEqual((FakeReceivedInstanceAction.KEEP_AS_IS, None), result)
        self.assertEqual([], ORTHANC.transcodeCalls)

    def test_keeps_original_when_transcoding_fails(self):
        ORTHANC.transcodeError = RuntimeError('transcode failed')

        result = MODULE.ReceivedInstanceCallback(b'original', None)

        self.assertEqual((FakeReceivedInstanceAction.KEEP_AS_IS, None), result)
        self.assertIn('transcode failed', ORTHANC.errors[0])

    def test_keeps_original_when_source_sop_instance_uid_is_missing(self):
        del ORTHANC.source.tags['SOPInstanceUID']

        result = MODULE.ReceivedInstanceCallback(b'original', None)

        self.assertEqual((FakeReceivedInstanceAction.KEEP_AS_IS, None), result)
        self.assertIn('SOPInstanceUID is missing', ORTHANC.errors[0])

    def test_keeps_original_when_transcoder_reuses_source_uid(self):
        ORTHANC.transcoded.data = MakeDicomBytes(SOURCE_SOP_INSTANCE_UID)

        result = MODULE.ReceivedInstanceCallback(b'original', None)

        self.assertEqual((FakeReceivedInstanceAction.KEEP_AS_IS, None), result)
        self.assertIn('did not generate a new SOPInstanceUID', ORTHANC.errors[0])

    def test_keeps_original_when_identity_changes(self):
        ORTHANC.source.tags['StudyInstanceUID'] = (
            '1.2.826.0.1.3680043.8.498.6'
        )

        result = MODULE.ReceivedInstanceCallback(b'original', None)

        self.assertEqual((FakeReceivedInstanceAction.KEEP_AS_IS, None), result)
        self.assertIn('StudyInstanceUID changed', ORTHANC.errors[0])

    def test_keeps_original_when_frame_count_changes(self):
        ORTHANC.source.frames = 9

        result = MODULE.ReceivedInstanceCallback(b'original', None)

        self.assertEqual((FakeReceivedInstanceAction.KEEP_AS_IS, None), result)
        self.assertIn('frame count changed', ORTHANC.errors[0])

    def test_keeps_original_when_lossy_metadata_is_missing(self):
        data = dcmread(BytesIO(ORTHANC.transcoded.data))
        del data.LossyImageCompression
        output = BytesIO()
        dcmwrite(output, data, write_like_original=False)
        ORTHANC.transcoded.data = output.getvalue()

        result = MODULE.ReceivedInstanceCallback(b'original', None)

        self.assertEqual((FakeReceivedInstanceAction.KEEP_AS_IS, None), result)
        self.assertIn('lossy compression metadata is missing', ORTHANC.errors[0])

    def test_keeps_original_when_derived_metadata_is_missing(self):
        data = dcmread(BytesIO(ORTHANC.transcoded.data))
        data.ImageType = ['ORIGINAL', 'PRIMARY']
        output = BytesIO()
        dcmwrite(output, data, write_like_original=False)
        ORTHANC.transcoded.data = output.getvalue()

        result = MODULE.ReceivedInstanceCallback(b'original', None)

        self.assertEqual((FakeReceivedInstanceAction.KEEP_AS_IS, None), result)
        self.assertIn('derived image metadata is missing', ORTHANC.errors[0])

    def test_rejects_unexpected_lossy_quality(self):
        ORTHANC.configuration = {'DicomLossyTranscodingQuality': 90}

        with self.assertRaisesRegex(RuntimeError, 'must be 70'):
            MODULE.ValidateConfiguration()

    def test_rejects_missing_pydicom_dependency(self):
        originalDcmread = MODULE.dcmread
        try:
            MODULE.dcmread = None
            with self.assertRaisesRegex(RuntimeError, 'pydicom is required'):
                MODULE.ValidateConfiguration()
        finally:
            MODULE.dcmread = originalDcmread

    def test_rejects_retranscoding_compressed_instances(self):
        ORTHANC.configuration = {
            'DicomLossyTranscodingQuality': 70,
            'IngestTranscoding': '1.2.840.10008.1.2.4.70',
        }

        with self.assertRaisesRegex(
            RuntimeError,
            'IngestTranscodingOfCompressed',
        ):
            MODULE.ValidateConfiguration()


if __name__ == '__main__':
    unittest.main()
