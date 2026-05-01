import React, { useMemo } from "react";
import {
  AbsoluteFill,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

// --- Types ---

export type Phrase = {
  words: { word: string; startMs: number; endMs: number }[];
  startMs: number;
  endMs: number;
};

export type CaptionOverlayProps = {
  captions: {
    wordTimestamps: { word: string; start: number; end: number }[];
    highlightColor: string;
    defaultColor: string;
    fontFamily: string;
    fontSize: number;
  };
};

// --- Phrase builder: sentence-aware grouping ---

// Group word timestamps into display phrases by sentence-ending punctuation.
// Each phrase = one subtitle screen (max 2 lines of ~5 words each).
export const buildPhrases = (
  wordTimestamps: { word: string; start: number; end: number }[]
): Phrase[] => {
  const phrases: Phrase[] = [];
  let current: Phrase["words"] = [];

  for (let i = 0; i < wordTimestamps.length; i++) {
    const wt = wordTimestamps[i];
    current.push({
      word: wt.word,
      startMs: Math.round(wt.start * 1000),
      endMs: Math.round(wt.end * 1000),
    });

    const endsWithPunctuation = /[.!?;]$/.test(wt.word.trim());
    const endsWithComma = /,$/.test(wt.word.trim()) && current.length >= 4;
    const tooLong = current.length >= 8;

    // Also break if there's a large time gap to the next word (segment boundary)
    const nextWt = i + 1 < wordTimestamps.length ? wordTimestamps[i + 1] : null;
    const bigGap = nextWt && (nextWt.start - wt.end) > 0.25;

    if (endsWithPunctuation || endsWithComma || tooLong || bigGap || i === wordTimestamps.length - 1) {
      phrases.push({
        words: [...current],
        startMs: current[0].startMs,
        endMs: current[current.length - 1].endMs,
      });
      current = [];
    }
  }

  return phrases;
};

const MAX_WORDS_PER_LINE = 5;

// --- Caption Overlay: progressive karaoke-style gold highlight ---

export const CaptionOverlay: React.FC<CaptionOverlayProps> = ({ captions }) => {
  const frame = useCurrentFrame();
  const { fps, width } = useVideoConfig();
  const timeSec = frame / fps;
  const timeMs = timeSec * 1000;

  // Build phrases from word timestamps (sentence-level grouping) — memoized to avoid
  // recomputing on every frame (30fps = 30 calls/sec with identical input)
  const phrases = useMemo(
    () => buildPhrases(captions.wordTimestamps),
    [captions.wordTimestamps],
  );

  // Find current phrase — each phrase is visible from its startMs through
  // its endMs plus a small natural hold (300ms for mid-video phrases, 1500ms
  // for the final phrase). During silences between phrases (weight≥2 gaps
  // carry 4-5s of silence), no caption is shown.
  //
  // Clamp rule: the hold must never extend past the NEXT phrase's startMs,
  // so a short gap (251–299ms) between phrases can't cause the old phrase
  // to overlap the new one — ``.find()`` would return the older one and
  // display stale text. buildPhrases splits on gaps > 0.25s (250ms), so
  // clamping to next.startMs guarantees non-overlapping visibility windows.
  const INTER_PHRASE_HOLD_MS = 300;
  const FINAL_PHRASE_HOLD_MS = 1500;
  const currentPhrase = phrases.find((p, idx) => {
    const isLast = idx === phrases.length - 1;
    const tail = isLast ? FINAL_PHRASE_HOLD_MS : INTER_PHRASE_HOLD_MS;
    const nextStart = !isLast ? phrases[idx + 1].startMs : Infinity;
    const visibleUntil = Math.min(p.endMs + tail, nextStart);
    return timeMs >= p.startMs && timeMs < visibleUntil;
  });

  if (!currentPhrase) return null;

  // Split into lines of MAX_WORDS_PER_LINE
  const words = currentPhrase.words;
  const lines: typeof words[] = [];
  for (let i = 0; i < words.length; i += MAX_WORDS_PER_LINE) {
    lines.push(words.slice(i, i + MAX_WORDS_PER_LINE));
  }

  const fontSize = captions.fontSize;

  return (
    <AbsoluteFill
      style={{
        justifyContent: "flex-end",
        alignItems: "center",
        paddingBottom: 380,
      }}
    >
      <div style={{ textAlign: "center", maxWidth: width * 0.9 }}>
        {lines.map((lineWords, lineIdx) => (
          <div
            key={lineIdx}
            style={{
              fontSize,
              fontWeight: "bold",
              fontFamily: `${captions.fontFamily}, Arial, sans-serif`,
              textTransform: "uppercase",
              letterSpacing: 2,
              lineHeight: 1.4,
            }}
          >
            {lineWords.map((w, wi) => {
              // Progressive karaoke: word turns gold once spoken, stays gold
              const spoken = w.startMs <= timeMs;

              return (
                <span
                  key={`${w.startMs}-${wi}-${lineIdx}`}
                  style={{
                    color: spoken ? captions.highlightColor : captions.defaultColor,
                    WebkitTextStroke: "2px rgba(0,0,0,0.6)",
                    paintOrder: "stroke" as const,
                    textShadow: "2px 2px 6px rgba(0,0,0,0.7)",
                  }}
                >
                  {w.word.toUpperCase()}
                  {wi < lineWords.length - 1 ? " " : ""}
                </span>
              );
            })}
          </div>
        ))}
      </div>
    </AbsoluteFill>
  );
};
