// v3 P3 "Hindi voice": the browser's own speech synthesis and recognition. The page sends no audio anywhere itself; what the browser does with
// speech recognition depends on the browser (Chrome sends audio to its provider's service). Nothing here is required: the chat works without it.
const W = window as any;

export const canSpeak = () => typeof window !== "undefined" && "speechSynthesis" in window;
export const canListen = () => typeof window !== "undefined" && !!(W.SpeechRecognition || W.webkitSpeechRecognition);

/** hindi and hinglish both read best with a Hindi voice for Devanagari; Roman-script Hinglish falls back to an Indian-English voice. */
export function speechLang(language: string, text: string): string {
  if (/[\u0900-\u097F]/.test(text)) return "hi-IN";
  return language === "english" ? "en-IN" : "en-IN";
}

export function speak(text: string, language: string): void {
  if (!canSpeak()) return;
  window.speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(text);
  u.lang = speechLang(language, text);
  const v = window.speechSynthesis.getVoices().find((x) => x.lang === u.lang) ?? window.speechSynthesis.getVoices().find((x) => x.lang.startsWith(u.lang.slice(0, 2)));
  if (v) u.voice = v;
  window.speechSynthesis.speak(u);
}

export function stopSpeaking(): void { if (canSpeak()) window.speechSynthesis.cancel(); }

export function listen(language: string, onText: (t: string) => void, onEnd: () => void): () => void {
  const R = W.SpeechRecognition || W.webkitSpeechRecognition;
  if (!R) { onEnd(); return () => {}; }
  const r = new R();
  r.lang = language === "english" ? "en-IN" : "hi-IN";
  r.interimResults = false;
  r.maxAlternatives = 1;
  r.onresult = (e: any) => onText(String(e.results[0][0].transcript));
  r.onend = onEnd;
  r.onerror = onEnd;
  r.start();
  return () => { try { r.stop(); } catch { /* already stopped */ } };
}
