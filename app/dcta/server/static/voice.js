/* Billie — push-to-talk voice input (Web Speech API).
   Hold the mic button to talk, release to send. Words appear in the input box as
   they are recognised. The top three alternative transcripts and their
   confidences go to the server with the message, so an amount the recogniser was
   unsure about ("fifty" or "fifteen") becomes a question instead of a guess.
   Note: the browser's built-in recogniser sends audio to the browser vendor;
   production would use a bank-controlled ASR. */
'use strict';

const Voice = (() => {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const MAX_ALTERNATIVES = 3;

  function init({ button, input, onFinal }) {
    if (!SR) { button.hidden = true; return; }   // text input still works
    button.hidden = false;
    let rec = null;

    function start(e) {
      e.preventDefault();
      if (rec) return;
      const finals = [];
      rec = new SR();
      Object.assign(rec, { lang: 'en-SG', interimResults: true, continuous: true,
                           maxAlternatives: MAX_ALTERNATIVES });
      rec.onresult = (ev) => {
        let interim = '';
        for (let i = ev.resultIndex; i < ev.results.length; i++) {
          if (ev.results[i].isFinal) finals.push(ev.results[i]);
          else interim += ev.results[i][0].transcript;
        }
        input.value = finals.map((r) => r[0].transcript).join(' ') + interim;
      };
      rec.onend = () => {
        rec = null;
        button.classList.remove('rec');
        if (!finals.length) return;
        // Alternative k = the k-th hypothesis of every segment, joined.
        const alternatives = [];
        for (let k = 0; k < MAX_ALTERNATIVES; k++) {
          const parts = finals.map((r) => r[Math.min(k, r.length - 1)]);
          const text = parts.map((p) => p.transcript.trim()).join(' ').trim();
          if (k > 0 && alternatives.some((a) => a.text === text)) continue;
          const confidence = parts.reduce((s, p) => s + (p.confidence || 0), 0) / parts.length;
          alternatives.push({ text, confidence: Math.round(confidence * 1000) / 1000 });
        }
        onFinal(alternatives[0].text, { engine: 'web-speech', lang: 'en-SG', alternatives });
      };
      rec.onerror = () => button.classList.remove('rec');
      rec.start();
      button.classList.add('rec');
    }

    const stop = () => { if (rec) rec.stop(); };
    button.addEventListener('pointerdown', start);
    ['pointerup', 'pointerleave', 'pointercancel'].forEach((t) => button.addEventListener(t, stop));
  }

  return { init };
})();
