import json

import orthanc


TARGET_TRANSFER_SYNTAX = '1.2.840.10008.1.2.4.50'
LOSSY_QUALITY = 70
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
    'SOPInstanceUID',
)


def GetTags(dicom):
    return json.loads(dicom.GetInstanceSimplifiedJson())


def IsOverwriteEnabled(value):
    return value is True or value in ('Always', 'IfChanged')


def ValidateConfiguration():
    configuration = json.loads(orthanc.GetConfiguration())

    if not IsOverwriteEnabled(configuration.get('OverwriteInstances')):
        raise RuntimeError(
            'OverwriteInstances must be true, Always, or IfChanged'
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


def GetDerivedImageType(tags):
    imageType = tags.get('ImageType')
    if isinstance(imageType, list):
        parts = imageType
    elif imageType:
        parts = imageType.split('\\')
    else:
        parts = ['DERIVED', 'PRIMARY']

    parts[0] = 'DERIVED'
    return '\\'.join(parts)


def ValidateTranscodedDicom(source, sourceTags, transcoded):
    transcodedTags = GetTags(transcoded)

    for tag in IDENTITY_TAGS:
        if (
            not sourceTags.get(tag)
            or transcodedTags.get(tag) != sourceTags.get(tag)
        ):
            raise ValueError(f'{tag} changed during transcoding')

    if transcoded.GetInstanceTransferSyntaxUid() != TARGET_TRANSFER_SYNTAX:
        raise ValueError('unexpected transfer syntax after transcoding')

    if transcoded.GetInstanceFramesCount() != source.GetInstanceFramesCount():
        raise ValueError('frame count changed during transcoding')

    if transcodedTags.get('LossyImageCompression') != '01':
        raise ValueError('lossy compression metadata is missing')

    if transcodedTags.get('LossyImageCompressionMethod') != 'ISO_10918_1':
        raise ValueError('lossy compression method metadata is missing')

    if not str(transcodedTags.get('ImageType', '')).startswith('DERIVED'):
        raise ValueError('derived image metadata is missing')


def BuildModification(tags):
    sopInstanceUid = tags.get('SOPInstanceUID')
    if not sopInstanceUid:
        raise ValueError('SOPInstanceUID is missing')

    return {
        'Transcode': TARGET_TRANSFER_SYNTAX,
        'Replace': {
            'SOPInstanceUID': sopInstanceUid,
            'ImageType': GetDerivedImageType(tags),
            'LossyImageCompression': '01',
            'LossyImageCompressionMethod': 'ISO_10918_1',
            'DerivationDescription': (
                f'Lossy JPEG compression at quality {LOSSY_QUALITY}'
            ),
        },
        'Force': True,
        'LossyQuality': LOSSY_QUALITY,
    }


def OnStoredInstance(dicom, instanceId):
    try:
        tags = GetTags(dicom)
        if tags.get('Modality') != 'US':
            return

        if dicom.GetInstanceFramesCount() <= 1 or IsLossy(dicom, tags):
            return

        transcodedBytes = orthanc.RestApiPost(
            f'/instances/{instanceId}/modify',
            json.dumps(BuildModification(tags)),
        )
        transcoded = orthanc.CreateDicomInstance(transcodedBytes)
        ValidateTranscodedDicom(dicom, tags, transcoded)

        uploadResponse = json.loads(
            orthanc.RestApiPost('/instances', transcodedBytes)
        )
        if uploadResponse.get('ID') != instanceId:
            orthanc.LogError(
                f'Transcoded instance {uploadResponse.get("ID")} does not '
                f'match source {instanceId}'
            )
            return

        orthanc.LogInfo(
            f'Transcoded multiframe US to JPEG Lossy: {instanceId}. '
            f'New size = {len(transcodedBytes)} vs {dicom.GetInstanceSize()}'
        )
    except Exception as e:
        orthanc.LogError(
            f'Keeping original DICOM after US transcoding failed: {e}'
        )


# This deliberately keeps the SOP Instance UID while changing the pixel data so
# the active object can be replaced. This is not DICOM-conformant and can make
# external systems retain the earlier object. Archive the original outside
# Orthanc before enabling it. Other stored-instance callbacks should ignore the
# REST-origin replacement if they must run only once per incoming instance.
try:
    ValidateConfiguration()
    orthanc.RegisterOnStoredInstanceCallback(OnStoredInstance)
except Exception as e:
    orthanc.LogError(f'US compression plugin disabled: {e}')
