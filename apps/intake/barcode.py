"""Optional local QR decoding for uploaded invoice images.

``zxing-cpp`` + ``pillow`` decode the QR client-free when they are installed;
when they are not, this returns ``None`` and the batch simply falls through to
the AI vision pipeline (or the caller ships the QR payload text directly,
which needs no image decoding at all).
"""
import io
import logging

logger = logging.getLogger(__name__)


def decode_qr_from_image(data):
    """First QR/barcode text found in raw image bytes, or ``None``."""
    try:
        import zxingcpp
        from PIL import Image
    except ImportError:
        return None
    try:
        image = Image.open(io.BytesIO(data))
        results = zxingcpp.read_barcodes(image)
    except Exception as exc:  # corrupted/unsupported image must never kill a batch
        logger.debug('QR decode failed: %s', exc)
        return None
    for result in results:
        if result.text:
            return result.text
    return None
