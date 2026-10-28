class AsrError(Exception):
    status_code = 500
    code = 'ASR_ERROR'

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class AudioUnsupportedError(AsrError):
    status_code = 415
    code = 'UNSUPPORTED_AUDIO'


class AudioTooLargeError(AsrError):
    status_code = 413
    code = 'AUDIO_TOO_LARGE'


class AudioTooLongError(AsrError):
    status_code = 413
    code = 'AUDIO_TOO_LONG'


class AudioEmptyError(AsrError):
    status_code = 400
    code = 'AUDIO_EMPTY'


class AsrUnavailableError(AsrError):
    status_code = 503
    code = 'ASR_UNAVAILABLE'


class AsrBusyError(AsrError):
    status_code = 429
    code = 'ASR_BUSY'


class AsrTimeoutError(AsrError):
    status_code = 504
    code = 'ASR_TIMEOUT'
