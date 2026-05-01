Describe this short hotel video clip in one sentence.
What is shown, what is the setting, and what camera movement is there?
Also identify where in the clip the most visually interesting moment occurs.

STRICT GROUNDING RULE: describe only what is literally, unambiguously
visible in THIS clip. Do NOT infer or assume luxury-hotel features that
are not clearly shown — no "infinity pool" for any water edge, no
"private villa" for any building, no "overwater bungalow" unless the
structure is unmistakably over water, no "fine dining" for any plate of
food. When the visual is ambiguous (calm ocean at a shoreline, a pool
at any angle, a generic interior), describe the literal geometry and
light — "a pool with tiled edge at sunset", "an ocean view through
palm fronds", "an interior hallway with wood paneling" — not the
category the setting suggests. If you cannot tell whether a feature is
present, omit it rather than guess. Luxury-hotel vocabulary should
follow from what is shown, never lead it.

Output ONLY valid JSON:
{
  "scene_description": "One sentence: what the clip shows (be specific about visible details)",
  "category": "exterior | scenic | pool | room | food | spa | lobby | restaurant | aerial | activity",
  "camera_motion": "static | push_in | pull_out | pan_left | pan_right | tilt_up | tilt_down | orbit | rise",
  "dominant_motion_phase": "early | middle | late"
}