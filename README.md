# od_raw_ll

Pair each OD corrosion LL window with the raw channel frames it was beamformed from, so a later model can run from raw data and skip beamforming.

This repo is the R&D record: the staged plan, the subset sampler, and the measurements. The heavy catalog is not here. It is built on `dv-hamed` under `catalog/` and copied to `s3://ili-ml-datasets/lancedb/research/hamed/od_raw_ll`.

## Read these first

| File | What it is |
|---|---|
| [plans/staged_build.plan.md](plans/staged_build.plan.md) | The build, stage by stage. |
| [plans/subset_sampler.plan.md](plans/subset_sampler.plan.md) | How the 24,000 volumes were chosen. |
| [artifacts/od_raw_ll_overview.jpg](artifacts/od_raw_ll_overview.jpg) | One-page picture of the subset and the tiers. |
| [artifacts/od_raw_ll_type_compare.jpg](artifacts/od_raw_ll_type_compare.jpg) | Catalog vs subset, by anomaly type. |
| [artifacts/manifest.parquet](artifacts/manifest.parquet) | The 24,000 selected volumes. |
| [artifacts/raw_timings_iq_up0.json](artifacts/raw_timings_iq_up0.json) | Stage 3: IQ without upsample (preferred form). |

`scripts/README.md`, `scripts/PLAN.md`, `scripts/catalog_census.json`, and `scripts/od_raw_ll_plan.pptx` describe a retired 26k catalog on `s3://dv-ml-data/...`. Do not use them as the source of truth.

## Where things stand (13 Nov 2026)

The 500-volume pilot is on the machine, in `catalog/` (not in git). Each volume has its LL image and its IQ frames. IQ is stored without upsample, shape `(128, 204, 2)` int16 per frame. That is the array the beamformer reads. The full 24,000 is not fetched yet.

| Stage | State |
|---|---|
| 0 Environment, 1 Manifest, 3 Raw timing | Done |
| 2 Prerequisites | P1, P2, and P3 pass. P3 locks IQ without upsample. |
| 2, P4 and P5 | `0AP3OOMEPID` matches the stored LL exactly. `0A1OLRV96N3` and `0A49KLT3B7Y` match after the float16 rounding those images were saved with. `0ABP0TFUSH1` and `0AGBXB4WEGN` do not: the wall does not land on the same pixels. Report: [artifacts/p4/p4_rebeamform.md](artifacts/p4/p4_rebeamform.md). |
| 4 and 5, pilot | LL and IQ for the 500 are written locally. Not validated. |
| 6 Validate pilot | Next. Then a decision on whether to fetch the remaining tiers. |
| 7 to 9 | Not started. |

`0ABP0TFUSH1` and `0AGBXB4WEGN` are in the pilot. Their re-beamform check is skipped until the ID-surface file that built their stored LL is identified.

## Figures

**Subset and tiers**

![Subset and fetch tiers](artifacts/od_raw_ll_overview.jpg)

**Anomaly type, catalog vs subset**

![Anomaly type share, catalog vs 24k subset](artifacts/od_raw_ll_type_compare.jpg)

## Source

`s3://ili-ml-datasets/lancedb/research/mahsa/kraken/od_corrosion/ll/merged_7neg_8_10_11_13`

Namespace `kraken/od_corrosion`. Tables `volumes`, `dataset`, `annotations`. Measured 12 Nov 2026: 61,005 volumes, 61,075 dataset rows, 13,781 OD annotations, 6.43 M frames, 5 inspections, 166 clips. All `mode=ll`, JP2, 441×201. About 141 GB on S3.

## Subset

24,000 volumes (20,000 train, 4,000 eval), 2,536,664 frames, all 166 clips. Train tiers 4k / 8k / 12k / 20k are nested. The pilot is 500 volumes inside the first tier. Two runs of the sampler wrote the same parquet (`9b00dbc8d7ae98efef75e53d85100c36a76390ae0cb3a2d72c91ec3f6d5d0417`).

The mix is 30% OD / 35% confuser / 35% background. It is not a miniature of the catalog: nominal falls from 41.6% to 23.8%, OD rises from 22.7% to 31.0%. See the type-compare figure.

## Stage 3: raw form measurement (13 Nov 2026)

Timed 20 pilot windows on each of RF, IQ without upsample (`iq_up0`), and IQ with upsample (`iq_up1`). All 60 reads succeeded. Frame shape is the same on all five inspections. The old 107 KiB/frame assumption was low for RF and close for `iq_up0`.

| Form | Shape per frame | KiB/frame | Cold read (median) | Warm read | Full 24,000 | First dataset (4k train + 4k eval) |
|---|---|---|---|---|---|---|
| RF | `(128, 680)` int16 | 170 | 2.6 s | 0.02 s | 17.4 h, 411 GiB | 5.8 h, 135 GiB |
| IQ, no upsample | `(128, 204, 2)` int16 | 102 | 0.5 s | 0.003 s | 3.2 h, 247 GiB | 1.1 h, 81 GiB |
| IQ, upsample | `(128, 680, 2)` int16 | 340 | 0.5 s | 0.03 s | 3.6 h, 823 GiB | 1.2 h, 270 GiB |

The old 107 KiB/frame assumption is low. RF is 170 KiB on every window, so the full raw set is about 411 GiB, not 259. IQ without upsampling is the small one, at 102 KiB. Upsampled IQ is the same IQ stretched onto the RF time grid, at 340 KiB.

The disk (about 779 GB free) can hold RF or non-upsampled IQ for all 24,000 volumes, plus the LL copy. Upsampled IQ at 823 GiB does not fit the full set. The first dataset fits all three forms.

IQ was timed right after RF on the same windows, so a little chunk-server cache may remain in those times. The sizes are exact. The hours are one reader; the real fetch uses several. A second read of the same window is about 100 times faster, which is why reading each clip in frame order matters.

**Stored form:** IQ without upsample (`iq_up0`). It is the smallest form that fits, and P3 showed it is the array the beamformer reads: handing those frames back in reproduces a normal beamform with difference 0. Upsampled IQ is rejected by the beamformer.

Per-window JSON: [raw_timings_rf.json](artifacts/raw_timings_rf.json), [raw_timings_iq_up0.json](artifacts/raw_timings_iq_up0.json), [raw_timings_iq_up1.json](artifacts/raw_timings_iq_up1.json).

Counts behind the type figure are in [artifacts/type_compare.json](artifacts/type_compare.json). The 20 timing windows are [artifacts/sample_windows.json](artifacts/sample_windows.json). Machine probes are [artifacts/probe_access.json](artifacts/probe_access.json).

## Context

Written so a collaborator can start from this repo without the design chat.

Beamforming is the expensive step in OD corrosion detection. The idea, from a whiteboard on 10 Nov 2026, is a catalog that holds both forms of the same frames: the LL image that already exists, and the raw channel data it was made from. Copy the LL volumes. Do not beamform them again. Fetch raw from the chunk server. Then try distillation: the production LL model is the teacher, a raw model is the student. A shared VAE embedding was the other option and is not the first path.

The first census used the wrong catalog, `s3://dv-ml-data/lancedb/user/mahsa/datasets` (26,361 volumes, 4 inspections, no lossless frames). The catalog this work uses is the merged one above. It adds `0AP3OOMEPID` (ILIT0013, more than half the rows) and lossless frames on nearly every eval volume.

All 61k volumes is the wrong R&D set. Raw for the whole catalog would be on the order of 650 GiB at the old 107 KiB/frame guess (about 620 GiB at the measured `iq_up0` rate), and 17,037 of the train nominal windows come from 19 clips of one inspection. The subset keeps every inspection and all 166 clips, spends budget on OD depth tails and on confusers (other corrosion, ID corrosion, lamination, inclusion, component), and cuts plain nominal. Cells are inspection by anomaly type. Each cell gets 50 volumes or the whole cell, then the rest of the group budget by the square root of cell size. Order inside a cell is round-robin over clips, and for OD over depth bins, with ties broken by `sha1("20261112:"+volume_id)`. Volumes over 330 frames are dropped. A volume with two or three annotations stays one row; depth is the max. Eval volumes without lossless frames are skipped, because a later check re-beamforms stored IQ and compares it to the lossless LL image. Tiers are nested, so a later fetch only adds volumes.

That mix is deliberate and it is not a copy of the catalog. See the table in the previous section. A proportional redraw would hold each type within about a point of the catalog share. That redraw has not been done.

Work runs on `dv-hamed` (`i-023016cf319153c36`, g5.2xlarge, one A10G). The host name `ec2-hamed` is retired. Build on the root disk (`/` had 777 GiB free on 13 Nov 2026, write about 890 MiB/s). Do not use `/opt/dlami/nvme`; it is wiped when the instance stops. The workflow checkout `~/core-learning/ILIWorkflows` is read-only. The Python env is `cache/venv` (gitignored). `uv sync --frozen` failed there because that checkout has no `uv.lock`. The installed LanceDB is 0.37.1, not 0.40.0; it opened the source catalog. PyTorch sees the A10G.

One clip opened on each of the five inspections. A one-row probe table was created and dropped under the destination bucket. Current status is in "Where things stand" above.
