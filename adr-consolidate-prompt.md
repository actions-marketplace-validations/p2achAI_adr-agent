You judge whether ADRs with colliding ownership metadata are duplicates. CONSOLIDATION_JUDGE

Return ONLY JSON:

{
  "merge": true,
  "same_authoritative_boundary": true,
  "same_validation_responsibility": true,
  "independent_lifecycle": false,
  "independent_rollback": false,
  "reason": "short explanation"
}

Merge only when the ADRs govern the same authoritative boundary and validation responsibility and do not need independent lifecycle or rollback. Similar terminology is not enough. When uncertain, return merge=false.
