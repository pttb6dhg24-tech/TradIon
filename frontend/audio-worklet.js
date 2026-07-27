/* TradIon — F5: AudioWorkletProcessor de captura de micrófono.
 *
 * Corre en el HILO DE AUDIO (no en el principal): re-muestrea del sampleRate
 * nativo del dispositivo (44100/48000 Hz típico en móviles) a PCM mono 16 kHz
 * por interpolación lineal, convierte a Int16 y envía bloques por MessagePort.
 *
 * Reglas:
 *  - Cero asignaciones por quantum (buffers pre-reservados): sin presión de GC
 *    en el hilo de audio.
 *  - Bloques de 2048 muestras @16 kHz = 128 ms por mensaje (~8 mensajes/s por
 *    el WebSocket; las ventanas VAD de 512 muestras del servidor encajan 4x).
 *  - Int16Array usa el endianness de la plataforma: little-endian en todo el
 *    hardware objetivo (ARM/x86), que es lo que espera el backend (PCM16LE).
 */

const TARGET_RATE = 16000;
const BLOCK_SAMPLES = 2048;

class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._ratio = sampleRate / TARGET_RATE; // `sampleRate` es global del scope del worklet
    this._inBuf = new Float32Array(32768);
    this._inLen = 0;
    this._pos = 0; // posición fraccional de lectura sobre _inBuf
    this._out = new Int16Array(BLOCK_SAMPLES);
    this._outLen = 0;
    this._ducking = 0;
    this._duckGain = 0.12;   // atenuar, NO silenciar: cero absoluto = medio-dúplex de facto
    this._duckBypass = false; // durante la captura del enroll el ducking se desactiva
    this.port.onmessage = (e) => {
      if (e.data.type === 'duck') {
        this._ducking += e.data.value ? 1 : -1;
        if (this._ducking < 0) this._ducking = 0;
      } else if (e.data.type === 'duck_bypass') {
        this._duckBypass = !!e.data.value;
      }
    };
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch || ch.length === 0) {
      return true; // el micrófono aún no entrega audio este quantum
    }

    // Compactar el buffer de entrada descartando lo ya consumido.
    // OJO: _pos puede quedar hasta ratio-1 muestras POR DELANTE de _inLen tras el
    // bucle de re-muestreo; sin el clamp, _inLen quedaría negativo y process()
    // lanzaría RangeError -> processorerror -> micrófono muerto en silencio.
    if (this._inLen + ch.length > this._inBuf.length) {
      const consumed = Math.min(Math.floor(this._pos), this._inLen);
      this._inBuf.copyWithin(0, consumed, this._inLen);
      this._inLen -= consumed;
      this._pos -= consumed;
    }
    this._inBuf.set(ch, this._inLen);
    this._inLen += ch.length;

    // Re-muestreo lineal: necesita las muestras i e i+1
    while (Math.floor(this._pos) + 1 < this._inLen) {
      const i = Math.floor(this._pos);
      const frac = this._pos - i;
      const sample = this._inBuf[i] * (1 - frac) + this._inBuf[i + 1] * frac;
      const clamped = Math.max(-1, Math.min(1, sample));
      const duckGain = (this._ducking > 0 && !this._duckBypass) ? this._duckGain : 1;
      this._out[this._outLen++] = Math.round(clamped * 32767 * duckGain);
      this._pos += this._ratio;

      if (this._outLen === BLOCK_SAMPLES) {
        const block = this._out.slice(); // copia transferible independiente
        this.port.postMessage(block.buffer, [block.buffer]);
        this._outLen = 0;
      }
    }
    return true;
  }
}

registerProcessor('tradion-capture', CaptureProcessor);
