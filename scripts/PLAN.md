> Superseded 12 Nov 2026. This file describes the retired 26k catalog.
> The build to follow is ../plans/staged_build.plan.md.
> The subset design is ../plans/subset_sampler.plan.md.

# Raw + LL paired dataset: implementation plan

Background and numbers: [README.md](README.md).

**Goal.** Build a Lance catalog that holds, for a subset of the mahsa OD corrosion samples, both the existing LL beamformed volume and the raw channel data for the same frames. Then use it to learn a representation shared between raw and LL.

**Where the code goes.** `ILIWorkflows/workflows/od_corrosion_detection`, on a new branch off master. Not on `feature-od-corrosion-chunk-prefetch`.

This plan stops short of step-by-step code, because four decisions below change what gets written. Each task names the files and the check that shows it worked.

## Decisions needed first

| # | Decision | Options | My suggestion |
|---|---|---|---|
| D1 | What "raw" is | RF frames from `ChannelDataReader.get_rf_frames` (ZeroAngle profile). IQ frames from `get_iq_frames`. Or the complex beamformed output, which the sampler already produces as `ll_cx`. | RF. It is the input beamforming reads, so it is the modality that would let us skip beamforming. `ll_cx` is still beamformed. |
| D2 | Subset size | 30% (about 7,900 volumes) or 10k volumes (38%). | 30%, stratified. |
| D3 | How the subset is drawn | Uniform random. Or stratified by inspection, split, and anomaly type, with all positives kept. | Stratified, all 2,325 positives kept. Positives are 9% of the data and a uniform 30% leaves about 700. |
| D4 | Raw codec | Uncompressed. Lossless (zstd). | Decide after `time_raw_window.py` reports the zlib ratio. The existing JP2 path is lossy for train and assumes a fixed 441 x 201 image, so it does not fit raw. |

## Phase 0. Measure before building

### Task 0: raw read timing

- Run `scripts/od_raw_ll/time_raw_window.py` (16 windows, read-only).
- Feed the result into `estimate.py`.
- **Check:** the script prints a median KiB per frame and a median cold seconds per volume. The estimate table no longer depends on the 107 KiB and 225 fps assumptions.
- **Stop if** the 30% subset is far above the 94 GiB assumed here. Then D2 or D4 needs revisiting first.

## Phase 1. Data prep

### Task 1: subset manifest

- Create `scripts/select_paired_subset.py`.
- Reads `dataset` and `volumes` metadata from the source catalog. Draws the subset per D2 and D3 with a fixed seed. Writes a parquet manifest: `sample_id`, `volume_id`, `inspection_id`, `clip_id`, `frame_start`, `n_frames`, `split`, `anomaly_type`.
- **Check:** running it twice gives the same manifest. Counts per inspection, split, and anomaly type are printed and match the target fractions. No volume appears twice. Train and eval stay on their original tracks, because `split` is copied, not redrawn.

### Task 2: new catalog with LL copied

- Create `scripts/copy_ll_subset.py`.
- Copies the manifest's rows from the source `volumes`, `annotations`, and `dataset` tables into a new catalog root, blobs included. No beamforming. Schemas come from `catalog_schemas.py` unchanged, and `volume_id` and `sample_id` are kept, so every row traces back to the source.
- New root, for example `s3://dv-ml-data/lancedb/user/hamed/od_raw_ll`. `OutputParameters` rejects any path containing `prod`.
- **Check:** row counts equal the manifest. For 20 random volumes, `frame_bytes` in the new catalog is byte-identical to the source. `ODCorrosionLanceDataset` opens the new root and yields a batch.

### Task 3: raw volumes

- Create `od_corrosion_detection/anomaly_sampling/raw_reader.py`: one function that returns the RF frames for `(clip, frame_start, n_frames)`. `AnomalySamplingRunner._reader_for` already builds a `ChannelDataReader` per clip. Chunk downloads can be queued ahead with `ChannelDataReader.queue_frames`, which is what `utils/chunk_prefetch.py` on the chunk-prefetch branch does.
- Add `raw_volumes_schema()` to `catalog_schemas.py`: `volume_id`, `inspection_id`, `clip_id`, `frame_start`, `n_frames`, `shape`, `dtype`, `codec`, `rf_decimation`, `created_at`, and one blob column.
- Create `scripts/write_raw_subset.py`: walks the manifest clip by clip, reads each window, writes one row per volume.
- **Why a separate table and not a new `mode`.** `volume_id` is a hash that includes `mode`, so a `mode="raw"` row would get a different id from its LL partner. A `raw_volumes` table keyed by the LL `volume_id` makes the pairing a plain join.
- Resumable: skip volumes already written. Parallel across clips, with the session cache sizes from the chunk-prefetch work (16 requests, 512 MiB HTTP cache).
- **Check:** unit test with a fake reader for the schema and the resume path. Then a 50-volume run, and its measured seconds per volume replaces the estimate before the full run.

### Task 4: validate the pairing

- Create `scripts/validate_paired_catalog.py`.
- **Check:** every `dataset` row has exactly one LL volume and one raw volume. `n_frames` and `frame_start` agree. For 20 random volumes, the stored raw equals a fresh read from the chunk server.
- One visual check: beamform one stored raw window and compare with its stored LL volume. That confirms the two really are the same frames.

### Task 5: paired loader

- Create `od_corrosion_detection/utils/paired_lance_dataset.py`, wrapping `ODCorrosionLanceDataset` so one item is `(ll_window, raw_window, label)` for the same frames.
- **Check:** a test that the two windows cover the same absolute frame indices under window jitter. A throughput number in samples per second, since raw is about 17 times larger per frame than the LL JP2.

## Phase 2. Modelling

Both options need Phase 1. They do not depend on each other.

### Option II: distillation (suggested first)

- Teacher M1: the existing DINOv2-LSTM on LL (`od_corrosion_detection:production`), frozen.
- Student M2: a new encoder on raw.
- Loss: match the teacher's per-frame probability and, optionally, its features.
- **Why first.** The teacher already exists and there is a direct number to report: the student's detection metric on the eval split against the teacher's, with no beamforming at inference.
- **Check:** student versus teacher on the 1,891 eval samples (171 positives), same metric as `eval_flow.py`.

### Option I: shared embedding (VAE)

- Two encoders, one latent space, reconstruction plus an alignment loss between the paired raw and LL latents.
- The board has a question mark here. It is the more open-ended of the two.
- **Check:** retrieval (given a raw window, is its own LL window the nearest in latent space?) and a linear probe for OD corrosion on the frozen latent.

## Risks

- **No pixel alignment.** Raw is channel by time. LL is angle by radius. Boxes in `bbox_img` do not transfer to raw. Frame-level labels (`hits`) do.
- **Raw size.** About 94 GiB uncompressed for 30%, unmeasured. It drives storage and loader speed.
- **Eval is small.** 171 positives in eval, about 51 if eval is also cut to 30%. D3 suggests keeping all positives for this reason.
- **Train LL is lossy JP2.** Only eval has the lossless family. A model that learns from LL inherits that.
