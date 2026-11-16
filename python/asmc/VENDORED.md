# Vendored: Advanced Semantic Memory Clustering (ASMC)

Copied from the author's ASMC library (MIT, see LICENSE in this folder). Only the
core V2 backend is included: the API wrapper, memory manager, STM, LTM, the
spatial-valence coordinate system and the anchor map. Viewer, benchmarks and
tests are left out.

This copy is upstream `main` as of 2026-11-04 (the `memory-fixes-2026-11`
branch, merged; github.com/SeanMurphy2k17/AdvancedSemanticMemoryClustering-ASMC),
which carries these changes and the GhostEngine working-tree fixes (atomic counter
writes, a generic-entity threshold, batched link resolution, line-cached
tagging, proper-noun entities). Each change was measured on a copy of a real
store before it was applied; the measurements are in the plugin author's notes
(`FablesBox/experiments/findings.md` on the machine it was built on) and the
diffs against upstream are kept beside them.

| file | change |
|---|---|
| `V2/spatialValenceCompute.py` | word cache resolved under `ASMC_STORE_DIR`, so the library never writes into the plugin folder |
| `V2/longTermMemory.py` | overlap hits (entity and content-word channels) interleaved with the FAISS hits in rank order instead of appended into leftover slots, which dropped them entirely once the index held more than k memories (self-query recall 0.14 to 0.92); content-word scores weighted by inverse document frequency; answer words indexed as well as prompt words; the stitcher links the nearest few (`ASMC_STITCH_NEIGHBOURS`, default 5, within `ASMC_STITCH_MAX_DIST`) instead of everything inside a fixed radius; a source memory with `ltm_id` 0 is no longer skipped; FAISS pinned to one thread at index load (`ASMC_FAISS_THREADS`), since the traversal's hundreds of tiny searches per query otherwise wait on OpenMP under load |
| `V2/memoryManager.py` | one lock around `queryMemory` and `addMemory` (callers that use threads raced an unlocked FAISS index); an over-full short-term window drained on open, skipping entries long-term memory already holds; exact-word overlap over the short-term window, which the inverted index does not cover; overlap hits passed in rank order, content words first; the window checkpointed after the long-term write |
| `V2/shortTermMemory.py` | the periodic checkpoint moved out of `addMemory` into `checkpoint_if_due()`, so the file on disk never names an entry that has already been promoted (an unclean death used to promote it twice and leave the window one over its maximum for good) |
| `V2/memorySpatial.py` | `responseWords` stored beside `contentWords` |

A store written by the unpatched library works as is; the plugin's daemon
indexes answer words for new entries only. To index existing ones, or to
repair a store (duplicates, an over-full window), the plugin author's
`migrate_store.py` does it in one pass with the owning process stopped.

## 2026-11-14: brought to the `whole` branch (c3def55 of the GhostEngine submodule lineage)

`ASMC_API.py`, `V2/longTermMemory.py` and `V2/memoryManager.py` replaced by the versions the three minds run on
(the other four files were already identical); the previous copies are in `.before-whole-2026-11-14/`. What
changes: nothing the store brings up is cut back to k (the merged valence + word list comes back whole); every
direct result is stitched, not the first three; a record with the same text as another is its own memory in the
chain; the word/entity channel is a real ask (`ASMC_OVERLAP_HITS`, the daemon sets 3) and the recent-window search
honours `layer1_count`; equal word scores break toward the memory most about the words, then the newest
(`ASMC_TIES`); a store with `MemoryStructures/PROTECTED` refuses to be cleared. Same day, the daemon stopped
shortening anything stored or shown (see its header).
