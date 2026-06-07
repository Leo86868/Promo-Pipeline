// Note: not using @remotion/captions createTikTokStyleCaptions — it merges too aggressively.
// Using custom phrase grouping by sentence punctuation instead (see shared/CaptionOverlay).
import {
  AbsoluteFill,
  CalculateMetadataFunction,
  OffthreadVideo,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
  interpolate,
  spring,
} from "remotion";
import { TransitionSeries, linearTiming } from "@remotion/transitions";
import { fade } from "@remotion/transitions/fade";
import { loadFont } from "@remotion/fonts";
import { z } from "zod";
import { CaptionOverlay, AudioMix } from "../shared";

// --- Font loading: @remotion/fonts registers FontFace and blocks render via delayRender ---

const MONTSERRAT_FONT_FAMILY = "Montserrat";

loadFont({
  family: MONTSERRAT_FONT_FAMILY,
  url: staticFile("Montserrat-Bold.ttf"),
  weight: "700",
});

// --- Transition config ---

const TRANSITION_FRAMES = 8;

// --- Schema: matches props.json from Python pipeline ---

const clipSchema = z.object({
  clipId: z.string(),
  file: z.string(),
  narration: z.string(),
  videoStart: z.number(),
  videoEnd: z.number(),
  trimStart: z.number(),
  trimEnd: z.number(),
});

const wordTimestampSchema = z.object({
  word: z.string(),
  start: z.number(),
  end: z.number(),
});

// AUTHORED pause windows from Python pipeline (Gemini pause_after_ms -> SSML
// breaks rendered by ElevenLabs v2). Sprint 07 emits these but does not consume
// them in the Remotion component — future sprints pick per-gap strategies.
// strategy enum (mirrors Python emitter comment):
//   "extend-current" | "pull-next" | "broll" | "data-card" | "slow-motion" | "map-reveal" | null
const pauseWindowSchema = z.object({
  startSec: z.number(),
  durationSec: z.number(),
  afterSegmentIdx: z.number(),
  strategy: z.string().nullable(),
});

export const hotelPromoSchema = z.object({
  meta: z.object({
    poiName: z.string(),
    location: z.string(),
    fps: z.number().default(30),
    width: z.number().default(1080),
    height: z.number().default(1920),
  }),
  clips: z.array(clipSchema),
  audio: z.object({
    narration: z.string().optional(),
    bgm: z.string().optional(),
    bgmVolume: z.number().default(0.35),
    bgmDuckedVolume: z.number().default(0.18),
    duckRampSec: z.number().default(0.3),
    pauseWindows: z.array(pauseWindowSchema).optional().default([]),
  }).optional(),
  captions: z.object({
    wordTimestamps: z.array(wordTimestampSchema).default([]),
    highlightColor: z.string().default("#D4AF37"),
    defaultColor: z.string().default("#FFFFFF"),
    fontFamily: z.string().default("Montserrat"),
    fontSize: z.number().default(48),
  }).optional(),
  segments: z.array(z.object({
    segment: z.number(),
    text: z.string(),
    startSec: z.number(),
    endSec: z.number(),
  })).optional(),
  anchor: z.object({
    enabled: z.boolean().default(false),
    text: z.string().default("BOOK HERE"),
    startSec: z.number().default(0),
    durationSec: z.number().default(4),
  }).optional(),
});

type HotelPromoProps = z.infer<typeof hotelPromoSchema>;

// --- Calculate duration from clips ---

export const calculateHotelPromoMetadata: CalculateMetadataFunction<
  HotelPromoProps
> = async ({ props }) => {
  const fps = props.meta.fps;
  const clips = props.clips;

  if (clips.length === 0) {
    return { fps, durationInFrames: 150 }; // 5s fallback
  }

  // Duration = last clip's videoEnd in frames, which equals the sum of raw
  // clip spans. Per-clip overlap additions and per-transition deductions
  // inside ClipSequence always cancel — whether or not a given boundary
  // emits a transition (post-Sprint-16 tiny-tail guard) — so the total
  // remains rawSpan-sum. No subtraction needed here.
  const lastClip = clips[clips.length - 1];
  return {
    fps,
    durationInFrames: Math.ceil(lastClip.videoEnd * fps),
  };
};

// --- Clip Sequence: renders clips with crossfade transitions ---

// Pure helper: raw video-span of clip i in frames, BEFORE transition overlap is
// added. Mirrors the per-clip span math in ClipSequence so the boundary-decision
// below is consistent with the sequence-duration computation.
//
// Note: uses Math.round, so spans at the exact TRANSITION_FRAMES boundary
// (8/30s ≈ 0.267s) may round to 7 or 8 depending on float accumulation in the
// upstream Python pipeline. The invariant still holds either way because
// `durationInFrames = rawSpan + overlap` stays >= TRANSITION_FRAMES when a
// transition is emitted (rawSpan>=8 and overlap=8 both hold). If exact
// 8-frame clips become common, switch the threshold check to a seconds-based
// comparison (TRANSITION_FRAMES - 0.5) / fps to disambiguate the boundary.
const rawClipSpanFrames = (
  clips: HotelPromoProps["clips"],
  i: number,
  fps: number,
): number => {
  const clipStart = i === 0 ? 0 : clips[i].videoStart;
  const clipEnd = i + 1 < clips.length
    ? clips[i + 1].videoStart
    : clips[i].videoEnd;
  return Math.max(1, Math.round((clipEnd - clipStart) * fps));
};

// Pure helper: decide whether to emit a TransitionSeries.Transition between
// clip[i] and clip[i+1]. Remotion enforces sequence.duration >= adjacent
// transition.duration (on BOTH sides). If either neighbor's raw span is below
// TRANSITION_FRAMES we fall back to a hard cut, avoiding the invariant crash
// triggered by tiny freeze-prevention bridge tails on long-profile renders.
export const shouldTransitionBetween = (
  clips: HotelPromoProps["clips"],
  i: number,
  fps: number,
): boolean => {
  if (i >= clips.length - 1) return false;
  return (
    rawClipSpanFrames(clips, i, fps) >= TRANSITION_FRAMES &&
    rawClipSpanFrames(clips, i + 1, fps) >= TRANSITION_FRAMES
  );
};

const ClipSequence: React.FC<{ clips: HotelPromoProps["clips"]; fps: number }> = ({
  clips,
  fps,
}) => {
  return (
    <AbsoluteFill>
      <TransitionSeries>
        {clips.map((clip, i) => {
          // Derive durationInFrames from videoStart deltas. First clip uses
          // clipStart=0 (absorbs any pre-narration gap, same as prior Sequence code).
          const rawSpan = rawClipSpanFrames(clips, i, fps);
          // A transition is emitted after clip i only if both clip i and clip
          // i+1 are long enough to satisfy Remotion's invariant. When skipped,
          // no overlap compensation is added either (the sequences abut as a
          // hard cut).
          const hasTransitionAfter = shouldTransitionBetween(clips, i, fps);
          const overlap = hasTransitionAfter ? TRANSITION_FRAMES : 0;
          const durationInFrames = rawSpan + overlap;
          const trimStartFrames = Math.round(clip.trimStart * fps);

          const src = clip.file.startsWith("http")
            ? clip.file
            : staticFile(clip.file);

          return [
            <TransitionSeries.Sequence key={clip.clipId} durationInFrames={durationInFrames}>
              <OffthreadVideo
                src={src}
                trimBefore={trimStartFrames}
                style={{ objectFit: "cover", width: "100%", height: "100%" }}
              />
            </TransitionSeries.Sequence>,
            hasTransitionAfter ? (
              <TransitionSeries.Transition
                key={`fade-${clip.clipId}`}
                presentation={fade()}
                timing={linearTiming({ durationInFrames: TRANSITION_FRAMES })}
              />
            ) : null,
          ];
        })}
      </TransitionSeries>
    </AbsoluteFill>
  );
};

// --- Anchor Overlay: animated arrow pointing to TikTok POI tag ---

const AnchorOverlay: React.FC<{
  anchor: NonNullable<HotelPromoProps["anchor"]>;
}> = ({ anchor }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const timeSec = frame / fps;

  const startSec = anchor.startSec;
  const endSec = startSec + anchor.durationSec;

  // Not visible outside the time window
  if (timeSec < startSec || timeSec >= endSec) return null;

  // Frames relative to anchor start
  const localFrame = frame - Math.round(startSec * fps);

  // Entrance: slide up + fade in over 12 frames
  const entrance = spring({ frame: localFrame, fps, config: { damping: 14, mass: 0.8 }, durationInFrames: 12 });

  // Gentle continuous pulse on the arrow (2Hz cycle)
  const pulsePhase = (localFrame / fps) * 2 * Math.PI * 1.5;
  const pulse = 1 + 0.06 * Math.sin(pulsePhase);

  // Arrow bounce — small vertical oscillation
  const bounceY = 3 * Math.sin(pulsePhase * 0.8);

  // Fade out in the last 0.5 seconds
  const fadeOut = interpolate(timeSec, [endSec - 0.5, endSec], [1, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  const opacity = entrance * fadeOut;

  // Down-pointing chevron arrow
  const arrowSvg = (
    <svg width="48" height="48" viewBox="0 0 48 48" fill="none">
      <path
        d="M12 16 L24 32 L36 16"
        stroke="#D4AF37"
        strokeWidth="5"
        strokeLinecap="round"
        strokeLinejoin="round"
        fill="none"
      />
    </svg>
  );

  return (
    <AbsoluteFill
      style={{
        opacity,
        pointerEvents: "none",
      }}
    >
      {/* CTA block: text + arrow pointing down toward POI tag */}
      <div
        style={{
          position: "absolute",
          left: 60,
          top: 1540 + bounceY,
          transform: `scale(${pulse * entrance})`,
          transformOrigin: "bottom left",
          display: "flex",
          flexDirection: "column",
          alignItems: "flex-start",
          gap: 0,
        }}
      >
        {/* Main CTA text */}
        <span
          style={{
            fontFamily: "Montserrat, Arial, sans-serif",
            fontSize: 38,
            fontWeight: "bold",
            color: "#D4AF37",
            textShadow: "2px 2px 6px rgba(0,0,0,0.85), 0 0 16px rgba(0,0,0,0.4)",
            letterSpacing: 2,
          }}
        >
          {anchor.text}
        </span>
        {/* Subtitle */}
        <span
          style={{
            fontFamily: "Montserrat, Arial, sans-serif",
            fontSize: 24,
            fontWeight: "bold",
            color: "rgba(255,255,255,0.9)",
            textShadow: "1px 1px 4px rgba(0,0,0,0.85)",
            letterSpacing: 1,
            marginTop: -2,
          }}
        >
          for 10% off
        </span>
        {/* Animated down-arrow */}
        <div style={{ marginTop: 4, marginLeft: 20 }}>
          {arrowSvg}
        </div>
      </div>
    </AbsoluteFill>
  );
};

// --- Main Composition ---

export const HotelPromo: React.FC<HotelPromoProps> = (props) => {
  return (
    <AbsoluteFill style={{ backgroundColor: "black" }}>
      <ClipSequence clips={props.clips} fps={props.meta.fps} />
      {props.captions && props.captions.wordTimestamps.length > 0 && (
        <CaptionOverlay captions={props.captions} />
      )}
      {props.anchor && props.anchor.enabled && (
        <AnchorOverlay anchor={props.anchor} />
      )}
      {props.audio && props.segments && (
        <AudioMix audio={props.audio} segments={props.segments} />
      )}
    </AbsoluteFill>
  );
};
