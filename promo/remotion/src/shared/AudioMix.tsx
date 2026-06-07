import React from "react";
import {
  Audio,
  interpolate,
  staticFile,
  useVideoConfig,
} from "remotion";

// --- Types ---

export type AudioMixProps = {
  audio: {
    narration?: string;
    bgm?: string;
    bgmVolume?: number;
    bgmDuckedVolume?: number;
    duckRampSec?: number;
  };
  segments: {
    segment: number;
    text: string;
    startSec: number;
    endSec: number;
  }[];
};

// --- Audio Mix: narration (full volume) + BGM (ducked across narration span) ---
//
// Sprint 08 simple-span ducking: BGM stays ducked continuously from the
// first segment's start through the last segment's end. Authored pauses
// between segments do NOT un-duck the BGM — a re-swell mid-pause feels
// wrong to the operator. Per-pauseWindow strategy dispatch is explicitly
// out of scope here (deferred to Phase 4 / D+E content enrichment).

export const AudioMix: React.FC<AudioMixProps> = ({ audio, segments }) => {
  const { fps } = useVideoConfig();

  const narrationSrc = audio.narration
    ? audio.narration.startsWith("http") ? audio.narration : staticFile(audio.narration)
    : null;

  const bgmSrc = audio.bgm
    ? audio.bgm.startsWith("http") ? audio.bgm : staticFile(audio.bgm)
    : null;

  const bgmNormal = audio.bgmVolume ?? 0.35;
  const bgmDucked = audio.bgmDuckedVolume ?? 0.18;
  const ramp = audio.duckRampSec ?? 0.3;

  // Simple-span volume callback: one interpolate across the full narration
  // envelope [segments[0].startSec, segments[last].endSec]. When segments is
  // empty (rare edge case), keep BGM at normal volume throughout.
  const narrationStart = segments.length > 0 ? segments[0].startSec : null;
  const narrationEnd = segments.length > 0 ? segments[segments.length - 1].endSec : null;

  const bgmVolume = (frame: number) => {
    if (narrationStart === null || narrationEnd === null) {
      return bgmNormal;
    }
    const t = frame / fps;
    return interpolate(
      t,
      [narrationStart - ramp, narrationStart, narrationEnd, narrationEnd + ramp],
      [bgmNormal, bgmDucked, bgmDucked, bgmNormal],
      { extrapolateLeft: "clamp", extrapolateRight: "clamp" },
    );
  };

  return (
    <>
      {narrationSrc && <Audio src={narrationSrc} volume={1.0} />}
      {bgmSrc && <Audio src={bgmSrc} volume={bgmVolume} />}
    </>
  );
};
