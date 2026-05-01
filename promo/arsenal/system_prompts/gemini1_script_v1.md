$system_prompt$feedback_block

HOTEL: $poi_name
LOCATION: $location
$hotel_description_block
$notable_details_block

Write exactly $segment_count narration segments. Aim for ~$target_word_midpoint words total.

VISUAL PACING:
Write in natural phrase-sized beats where a visual cut would feel right.
You do NOT choose clips in this step — the next pipeline stage assigns clips
to your phrases based on real narration timing. Do not emit clip IDs.

SEGMENT STRUCTURE:
$segment_structure

RULES:
- $sentence_rule$extra_rules_block
- Use "you" and "your" freely. Contractions always.
- One specific number or fact per script (price, year built, measurement, count).
- React to what's shown, don't describe it. "The water's warm" not "a heated pool."
- NEVER use: $banned_phrases
- No rhetorical questions. No passive voice. No marketing speak.
- GROUNDING: Every physical detail you mention MUST be visible in the clip inventory shown below. If no clip shows a fireplace, do not write about a fireplace. If no clip shows a forest, do not mention forests. You may describe feelings and reactions to what's shown, but never invent physical features not in the inventory.
- WORD FORM FOR SPOKEN NUMBERS: Write currency and numerals in words where a narrator would say them out loud: "1,900 dollars" not "$$1,900", "ten percent off" not "10% off", "forty degrees" not "40°". Narration is fed to TTS verbatim; symbols confuse the voice model.
$variant_note$pause_block

$examples

VIDEO CLIP INVENTORY (visual grounding reference — describe only details visible in these clips; do NOT reference clip IDs in your output):
$clip_inventory

Output ONLY valid JSON:
{
  "segments": [
    {
      "segment": 1,
      "text": "First sentence. Second sentence. Third phrase.",
      "word_count": 18,
      "pause_weight": 2
    },
    ...repeat for the remaining segments (the final segment's pause_weight is ignored)
  ],
  "total_words": N,
  "hook_technique": "brief label: contradiction | sensory | specific_number | second_person",
  "unique_detail": "the one detail specific to this hotel"
}