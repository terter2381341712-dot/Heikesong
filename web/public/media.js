import { t } from './i18n.js';
export const mimeLabels = { 'image/jpeg': 'JPG', 'image/png': 'PNG', 'audio/wav': 'WAV' };
export const emptyMedia = {
  width: null, height: null, duration_ms: null, sample_rate_hz: null, channels: null, audio_encoding: null,
};

export class InputError extends Error {
  constructor(message, code = 'INPUT_INVALID') {
    super(message);
    this.code = code;
  }
}

export function getInputMime(file) {
  const declared = file.type.toLowerCase();
  if (declared === 'audio/x-wav' || declared === 'audio/wave') return 'audio/wav';
  if (declared) return declared;
  const extension = file.name.split('.').at(-1).toLowerCase();
  return { jpg: 'image/jpeg', jpeg: 'image/jpeg', png: 'image/png', wav: 'audio/wav' }[extension];
}

function parsePng(view) {
  if (view.byteLength < 33 || view.getUint32(0) !== 0x89504e47 || view.getUint32(4) !== 0x0d0a1a0a ||
    view.getUint32(8) !== 13 || view.getUint32(12) !== 0x49484452) return null;
  return { width: view.getUint32(16), height: view.getUint32(20) };
}

const jpegSizeMarkers = new Set([0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf]);

function parseJpeg(view) {
  if (view.byteLength < 4 || view.getUint16(0) !== 0xffd8) return null;
  let cursor = 2;
  while (cursor + 4 <= view.byteLength && view.getUint8(cursor++) === 0xff) {
    while (cursor < view.byteLength && view.getUint8(cursor) === 0xff) cursor++;
    if (cursor + 3 > view.byteLength) break;
    const marker = view.getUint8(cursor++);
    if (marker === 0xda || marker === 0xd9) break;
    const length = view.getUint16(cursor);
    if (length < 2 || cursor + length > view.byteLength) break;
    if (jpegSizeMarkers.has(marker) && length >= 8) {
      return { width: view.getUint16(cursor + 5), height: view.getUint16(cursor + 3) };
    }
    cursor += length;
  }
  return null;
}

function parseWav(view) {
  if (view.byteLength < 44 || view.getUint32(0) !== 0x52494646 || view.getUint32(8) !== 0x57415645) return null;
  const riffEnd = 8 + view.getUint32(4, true);
  if (riffEnd > view.byteLength) return null;
  let format;
  let dataBytes;
  for (let cursor = 12; cursor + 8 <= riffEnd;) {
    const tag = view.getUint32(cursor);
    const length = view.getUint32(cursor + 4, true);
    const start = cursor + 8;
    if (start + length > riffEnd) return null;
    if (tag === 0x666d7420 && length >= 16) {
      format = {
        encoding: view.getUint16(start, true),
        channels: view.getUint16(start + 2, true),
        sample_rate_hz: view.getUint32(start + 4, true),
        bytes_per_second: view.getUint32(start + 8, true),
        block_align: view.getUint16(start + 12, true),
        bits: view.getUint16(start + 14, true),
      };
    }
    if (tag === 0x64617461) dataBytes = length;
    cursor = start + length + (length % 2);
  }
  if (!format || !dataBytes || dataBytes % 2) return null;
  if (format.encoding !== 1 || format.bits !== 16 || format.channels !== 1 ||
    format.sample_rate_hz !== 16000 || format.block_align !== 2 || format.bytes_per_second !== 32000) {
    throw new InputError(t('WAV 必须为 16 kHz、单声道、PCM 16 位。', 'WAV files must use 16 kHz mono 16-bit PCM.'));
  }
  return {
    duration: dataBytes / 32,
    sample_rate_hz: format.sample_rate_hz,
    channels: format.channels,
    audio_encoding: 'PCM_S16LE',
  };
}

export function inspectInput(buffer, mime, descriptor) {
  const view = new DataView(buffer);
  if (mime === 'audio/wav') {
    const audio = parseWav(view);
    if (!audio) throw new InputError(t('无法读取有效的 WAV 音频。', 'Could not read valid WAV audio.'));
    const { duration, ...fields } = audio;
    if (descriptor.max_duration_ms != null && duration > descriptor.max_duration_ms) {
      throw new InputError(t('录音时长超出任务上限。', 'The audio exceeds the duration limit.'), 'INPUT_TOO_LARGE');
    }
    return { ...emptyMedia, ...fields, duration_ms: Math.ceil(duration) };
  }
  const image = mime === 'image/png' ? parsePng(view) : parseJpeg(view);
  if (!image?.width || !image.height) throw new InputError(t('无法读取有效的图片尺寸。', 'Could not read valid image dimensions.'));
  if (descriptor.max_pixels != null && image.width * image.height > descriptor.max_pixels) {
    throw new InputError(t('图片像素数超出任务上限。', 'The image exceeds the pixel limit.'), 'INPUT_TOO_LARGE');
  }
  return { ...emptyMedia, ...image };
}
