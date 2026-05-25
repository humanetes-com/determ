This document describes the way we plan to launch the pipeline.

currently the pipeline is trigered from the cli. we expect the pipeline to be trigered repositories owners in github.

a repository owner that wants to have his code coverage automatically brought up to 100% will add our repository as upstream and create a MR with the repository into ours. If the MR has in the title [unit test augmentation] it will be augmented.

we need to create a backend with two components. The (help me find a better name is not the scheduler, perhaps a tasks queue simply) that lists all MR created against all repository and creates a queue of all [unit test augmentation] tasks sorted by creation date.

another component is the worker that lists the top in the queue and send it to the pipeline. the worker will also check the pipeline health and take actions.

when the augmentation task is completed(task0c is succesful), an mr will be sent against downstream that created a MR in our repo

for now I would not implement the other kind of tasks. only augmentation 

---

## Architecture

### Components

**intake** — monitors all PRs opened against our repo. filters for title containing
`[unit test augmentation]`. maintains a queue sorted by creation date. each entry
holds: PR number, fork repo url, fork branch name, sha at intake time.

**runner** — polls the top of the queue, sends the job to the pipeline, monitors
health, logs events. one job at a time for now.

**pipeline** — existing pipeline.py, invoked by runner with the fork repo url and
branch. runs task0c only (augmentation mode). pushes augmented test commits directly
to the fork branch — they appear automatically in the original PR.

### PR flow

1. repo owner opens PR from their fork into our repo, title includes `[unit test augmentation]`
2. intake picks it up, adds to queue
3. runner claims the job, clones the fork branch, runs pipeline
4. pipeline pushes commits with augmented tests back to the fork branch
5. commits appear in the original PR automatically (standard GitHub fork behaviour)
6. no separate PR needed, no GitHub App installation required on their side
7. requires "allow edits from maintainers" enabled on the PR (GitHub default)

### Termination conditions

loop until one of:
- coverage reaches 100%
- 10 consecutive iterations with less than 0.1% improvement
- wall-clock timeout (2 hours per job) — prevents pathological repos holding the runner

when terminated: if any improvement was made, the PR already contains the commits.
no further action needed — the contributor sees the result in their PR.

### Health event log

runner logs events to the existing events.jsonl. events to capture:
- pipeline process died unexpectedly
- pipeline exceeded timeout
- cargo build never succeeded after N iterations
- coverage tool failed
- git push to fork branch failed

v1: events are reviewed manually. automated recovery actions deferred.

### Plan gate (intake checks before queuing)

checks run on the PR's head commit before the job enters the queue:

| check | reason |
|---|---|
| Cargo.toml present at root | rust-only, pipeline requires cargo |
| at least one #[test] or #[cfg(test)] | task0c augments existing tests, cannot start from zero |
| cargo build succeeds | no point running if it doesn't compile |
| source file count under 50 .rs files | v1 scope limit, prevents multi-hour jobs |

failed checks: post a comment on the PR explaining which gate failed and close it.
license: not checked — contributor's responsibility.

### Deferred

- multi-language support
- reconstruction mode (task1-4)
- parallel runners
- automated health recovery
- authentication / plan gating beyond the rust check
