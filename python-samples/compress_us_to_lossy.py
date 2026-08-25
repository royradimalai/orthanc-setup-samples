from io import BytesIO
import json
import uuid

import orthanc

try:
    from pydicom import dcmread, dcmwrite
    from pydicom.dataset import Dataset
except ImportError:
    dcmread = None
    dcmwrite = None
    Dataset = None


TARGET_TRANSFER_SYNTAX = '1.2.840.10008.1.2.4.50'
LOSSY_QUALITY = 70
COMPRESSION_PROFILE_VERSION = 1
LOSSY_TRANSFER_SYNTAXES = {
    '1.2.840.10008.1.2.4.50',
    '1.2.840.10008.1.2.4.51',
    '1.2.840.10008.1.2.4.81',
    '1.2.840.10008.1.2.4.91',
    '1.2.840.10008.1.2.4.203',
}
IDENTITY_TAGS = (
    'PatientID',
    'StudyInstanceUID',
    'SeriesInstanceUID',
    'SOPClassUID',
)


def GetTags(dicom):
    return json.loads(dicom.GetInstanceSimplifiedJson())


def ValidateConfiguration():
    if dcmread is None or dcmwrite is None:
        raise RuntimeError('pydicom is required for US compression')

    configuration = json.loads(orthanc.GetConfiguration())
    quality = configuration.get('DicomLossyTranscodingQuality', 90)
    if quality != LOSSY_QUALITY:
        raise RuntimeError(
            f'DicomLossyTranscodingQuality must be {LOSSY_QUALITY}, got {quality}'
        )

    if (
        configuration.get('IngestTranscoding')
        and configuration.get('IngestTranscodingOfCompressed', True)
    ):
        raise RuntimeError(
            'IngestTranscodingOfCompressed must be false when '
            'IngestTranscoding is configured'
        )


def IsLossy(dicom, tags):
    return (
        dicom.GetInstanceTransferSyntaxUid() in LOSSY_TRANSFER_SYNTAXES
        or tags.get('LossyImageCompression') == '01'
    )


def IsDerived(tags):
    imageType = tags.get('ImageType')
    if isinstance(imageType, list):
        return bool(imageType) and imageType[0] == 'DERIVED'
    return str(imageType or '').split('\\')[0] == 'DERIVED'


def BuildDeterministicSopInstanceUid(sourceSopInstanceUid):
    profile = '|'.join((
        sourceSopInstanceUid,
        TARGET_TRANSFER_SYNTAX,
        str(LOSSY_QUALITY),
        str(COMPRESSION_PROFILE_VERSION),
    ))
    return f'2.25.{uuid.uuid5(uuid.NAMESPACE_URL, profile).int}'


def BuildDerivedImageType(imageType):
    if imageType is None:
        return ['DERIVED', 'PRIMARY']

    values = (
        list(imageType)
        if not isinstance(imageType, str)
        else imageType.split('\\')
    )
    if not values:
        return ['DERIVED', 'PRIMARY']

    values[0] = 'DERIVED'
    return values


def PrepareTranscodedDicom(transcodedBytes, sourceSopInstanceUid):
    dataset = dcmread(BytesIO(transcodedBytes))
    generatedSopInstanceUid = str(dataset.SOPInstanceUID)
    if generatedSopInstanceUid == sourceSopInstanceUid:
        raise ValueError('lossy transcoding did not generate a new SOPInstanceUID')

    deterministicSopInstanceUid = BuildDeterministicSopInstanceUid(
        sourceSopInstanceUid
    )
    dataset.SOPInstanceUID = deterministicSopInstanceUid

    if not getattr(dataset, 'file_meta', None):
        raise ValueError('transcoded DICOM is missing file metadata')
    dataset.file_meta.MediaStorageSOPInstanceUID = deterministicSopInstanceUid

    dataset.ImageType = BuildDerivedImageType(
        getattr(dataset, 'ImageType', None)
    )
    dataset.LossyImageCompression = '01'
    dataset.LossyImageCompressionMethod = 'ISO_10918_1'
    dataset.DerivationDescription = (
        f'Lossy JPEG compression at quality {LOSSY_QUALITY}'
    )

    sourceReference = Dataset()
    sourceReference.ReferencedSOPClassUID = dataset.SOPClassUID
    sourceReference.ReferencedSOPInstanceUID = sourceSopInstanceUid
    dataset.SourceImageSequence = [sourceReference]

    output = BytesIO()
    dcmwrite(output, dataset, write_like_original=False)
    return output.getvalue(), deterministicSopInstanceUid


def ValidateTranscodedDicom(
    source,
    sourceTags,
    transcoded,
    expectedSopInstanceUid,
):
    transcodedTags = GetTags(transcoded)

    for tag in IDENTITY_TAGS:
        if (
            not sourceTags.get(tag)
            or transcodedTags.get(tag) != sourceTags.get(tag)
        ):
            raise ValueError(f'{tag} changed during transcoding')

    if transcodedTags.get('SOPInstanceUID') != expectedSopInstanceUid:
        raise ValueError('unexpected SOPInstanceUID after transcoding')

    if transcoded.GetInstanceTransferSyntaxUid() != TARGET_TRANSFER_SYNTAX:
        raise ValueError('unexpected transfer syntax after transcoding')

    if transcoded.GetInstanceFramesCount() != source.GetInstanceFramesCount():
        raise ValueError('frame count changed during transcoding')

    if transcodedTags.get('LossyImageCompression') != '01':
        raise ValueError('lossy compression metadata is missing')

    if transcodedTags.get('LossyImageCompressionMethod') != 'ISO_10918_1':
        raise ValueError('lossy compression method metadata is missing')

    if not IsDerived(transcodedTags):
        raise ValueError('derived image metadata is missing')


def ReceivedInstanceCallback(receivedDicom, origin):
    try:
        source = orthanc.CreateDicomInstance(receivedDicom)
        sourceTags = GetTags(source)

        if sourceTags.get('Modality') != 'US':
            return orthanc.ReceivedInstanceAction.KEEP_AS_IS, None

        if source.GetInstanceFramesCount() <= 1 or IsLossy(source, sourceTags):
            return orthanc.ReceivedInstanceAction.KEEP_AS_IS, None

        sourceSopInstanceUid = sourceTags.get('SOPInstanceUID')
        if not sourceSopInstanceUid:
            raise ValueError('SOPInstanceUID is missing')

        transcoded = orthanc.TranscodeDicomInstance(
            receivedDicom,
            TARGET_TRANSFER_SYNTAX,
        )
        transcodedBytes, deterministicSopInstanceUid = PrepareTranscodedDicom(
            transcoded.SerializeDicomInstance(),
            sourceSopInstanceUid,
        )
        validated = orthanc.CreateDicomInstance(transcodedBytes)
        ValidateTranscodedDicom(
            source,
            sourceTags,
            validated,
            deterministicSopInstanceUid,
        )

        orthanc.LogInfo(
            f'Transcoded multiframe US to JPEG Lossy before storage: '
            f'{len(receivedDicom)} bytes to {len(transcodedBytes)} bytes'
        )
        return orthanc.ReceivedInstanceAction.MODIFY, transcodedBytes
    except Exception as e:
        orthanc.LogError(
            f'Keeping original DICOM after US transcoding failed: {e}'
        )
        return orthanc.ReceivedInstanceAction.KEEP_AS_IS, None


# Install requirements-compress-us-to-lossy.txt and use Orthanc Python plugin
# 4.0 or newer. The deterministic derived SOP UID prevents retransmission
# duplicates.
try:
    ValidateConfiguration()
    orthanc.RegisterReceivedInstanceCallback(ReceivedInstanceCallback)
except Exception as e:
    orthanc.LogError(f'US compression plugin disabled: {e}')
