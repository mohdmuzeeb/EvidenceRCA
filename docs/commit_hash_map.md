# Commit hash map

On 2026-11-01, before this repository was first published, its history was rewritten once. The rewrite did three things:

- corrected the author identity (the name had been recorded as `“Muzeeb`);
- removed `Co-Authored-By` trailers;
- removed the three sealed answer keys of the blinded human audit, so that publishing the repository does not unblind the audit.

Apart from those three files, every commit's tree is byte-identical to the original. These are the removed files:

- `results/phase4b/audit_key_sealed.json`
- `results/phase4a_v2/audit/audit_key_dev_pilot.SEALED.json`
- `results/phase4a_v3/audit_dry_run/key.SEALED.json`

Manifests that list checksums for them are unchanged. The keys are held privately by the author and will be released after the audit is adjudicated.

Docs, results and tests written before that date cite the **original** hashes, for example the freeze commit `912085c` in `docs/frozen/FREEZE_COMMIT` and `source_commit: 12af836` in `results/manuscript_prep`. Those files are left unchanged so their recorded checksums stay valid. Use this table to find each cited commit in the published history.

| Date | Original hash | Published hash | Commit |
|---|---|---|---|
| 2026-10-12 | `4aa0b4f` | `f2b45df` | Phase 0 docs and Phase 1 testbed with telemetry validation |
| 2026-10-12 | `db5531c` | `eb8525b` | Environment manifest: dirty flag covers tracked files only |
| 2026-10-12 | `dec9d67` | `b64d852` | Phase 1 results: run phase1-20261012T044845Z passes all checks |
| 2026-10-12 | `20a1e8f` | `1209fc2` | Phase 2: workload generator, fault injection, ground truth and leakage controls |
| 2026-10-12 | `1925e33` | `d88983d` | Phase 2 results: pilot 8/8 valid, mechanism checks 6/6, leakage checks pass |
| 2026-10-13 | `bec858e` | `3c15664` | Phase 3: baselines B1-B3, EvidenceRCA (no LLM), verifier V0-V6, abstention, views |
| 2026-10-13 | `98f6a8e` | `c52cf73` | Phase 3 results (dev seed 11 only) and documentation |
| 2026-10-13 | `912085c` | `3007725` | Freeze experiment protocol for calibration/test capture |
| 2026-10-13 | `8b2175d` | `64e53eb` | Record freeze commit hash |
| 2026-10-13 | `bb42bd3` | `96ec906` | Pre-capture check: compare service image inputs; report host-only uv.lock change explicitly |
| 2026-10-13 | `5a9fd02` | `bd716df` | Pre-capture leakage regression (re-run on frozen tree) |
| 2026-10-14 | `fb6a9fd` | `b518f94` | Calibration (seeds 23/37 only): tau fixed before any test label is read |
| 2026-10-14 | `7b54850` | `dfc6bb8` | Held-out test evaluation of the frozen deterministic methods |
| 2026-10-14 | `eeb18fc` | `92a1233` | Phase 4A: local LLM (qwen3:4b) setup, B4/E1 integration, five-call dev benchmark |
| 2026-10-14 | `c87ce77` | `c00defd` | Phase 4A-v2 pre-registration: p4a-v2 typed prompt/schema and dev pilot plan (no model call yet) |
| 2026-10-14 | `5cfee5f` | `4041ac8` | Phase 4A-v2 dev pilot results: p4a-v1 vs p4a-v2 (80 planned trials, dev seed 11) |
| 2026-10-14 | `9a8a944` | `3b12e99` | Phase 4A-v3 pre-registration: E2 deterministic evidence ownership, B4-G detector gate (no new call yet) |
| 2026-10-14 | `00f6777` | `625cfa3` | Phase 4A-v3 dev results: E2 evidence ownership and B4-G detector gate |
| 2026-10-14 | `b5bf532` | `4ef1158` | Phase 4B pre-registration: test-seed matrix (512 calls), exact request hashes, metrics, audit plan (no model call yet) |
| 2026-10-15 | `c0400e7` | `2de4d95` | Phase 4B results: B4/E1 on test seeds (512 calls), E2/B4-G from stored outputs, blinded audit package (pending) |
| 2026-10-15 | `12af836` | `6914eed` | Store the Phase 4B audit CSV byte-exact (CRLF kept) so it matches its manifest checksum |
| 2026-10-15 | `82948e1` | `200037c` | Manuscript preparation: human-audit workflow, IEEE tables/figures from recorded results, manuscript outline |
| 2026-10-16 | `50e73c4` | `9fe2fbf` | Architecture diagram (Graphviz, pinned layout) and validation against the implementation |
| 2026-10-16 | `75f2983` | `cc7e330` | Simplify the architecture diagram: fewer boxes, shorter labels, larger text; pin PDF date for byte-identical rebuilds |

Full 40-character hashes (original, published):

```
4aa0b4f7c5b6f9367058cc4a49c7ed83e7feff9a f2b45df6068685790ce4b61ea1086c60cc76fde7
db5531c31c2931752f528c5fa9b8155ce74665fc eb8525b00af76beafaf47d6988119a889edbad20
dec9d673700d3a49ec96c6e3ff1e84429f459d2c b64d85234d90bf82e00e3c2fcefd9b01ac0cdb93
20a1e8f02411e9e5ddbc0b07caed2899438cef73 1209fc21d82de1cf6eeb90bd30ffbdd18e2ae9b9
1925e33bbe10ad811533c652af0fe1c5e5178c4b d88983df024aa1ea60541a6d5810fbd3e60555a2
bec858ed951201bc873a8b00f81d37542638cc14 3c15664132db29188eae5f7a0662cf7051d92ec7
98f6a8e800a2b0e477ff614d3e88234b5ac5bb8e c52cf73f87c7dc163c0dccb3620f76ec91e5047d
912085c278bddaca8fcd79400af830d7e54b2819 30077258c9a148c00903f38ef55a951262d8018c
8b2175dbf21a1a44fe9514d8caac1a3d3070f524 64e53eb9eb03637aca24f14e5ade9ded7009425c
bb42bd3bb45a0411a6285973abf2942d7d353846 96ec9068baa010ded2bdadf7b60c8440935cc8c2
5a9fd02dec4f6dc4acf4596578f27a83422ad4d5 bd716df03633f50eaf00ee64e84ad7abcd57a56c
fb6a9fda8d4ea2a9ac9a237139948f44b073f53a b518f94cfeedbc2e2125f54bcb9bfab0480bd76c
7b5485019726a323786bd3cea58d14e2295d59da dfc6bb86c49645596459e54833c7aa406465ae5c
eeb18fcf72aeefda056c6abf5602dbca34fc1db2 92a12336dcb352e22ca5d6ee33d9a32d904d2d0b
c87ce776cf7c3acabdcc9a394f2919a5a57c46d4 c00defdef46345ae3b08923c184f460e0120275f
5cfee5f27b4215824314a1405d649e3e2233034d 4041ac885a864e59f7284fd8e5db501c012cf2cd
9a8a944b3e492f546be51c8f0014ca3e1085fce9 3b12e999810cbdb160a866e8644b90d2a2917045
00f6777a07e6584a7e9095d6c123488d5cdebc85 625cfa34dd637a9a7f809561e979d163a5a059fd
b5bf5329cfc38e6411eaa7a1e07ce4efe411b106 4ef11580afdc3a0b552b6c19ad29b41834bc1a48
c0400e7df5af432527af2aa2aa46022d854685b9 2de4d9537d442a8a661d7d2ded567d83da95a652
12af8367b47efc3b2b2cddbacfb31450095fdfb7 6914eed086f80454883786ed87be10fdfdbb563d
82948e10f053864cf147239697890ae51004184f 200037c60e1423265c890b9d6529eaf1ed893522
50e73c4174226f18822c91ef9cd0b44373367a9f 9fe2fbf638b1415dd9d8026655cfec4120879234
75f29837481c68fdd3b2f784f5c27f233e0a5316 cc7e3303dc0b672501d650863ab59c9efc629044
```
