# PCF pre-listener boot receipt candidate

This package replays the pinned free5GC PCF source commit and the complete
CityVerse candidate patch. It is an offline build recipe, not a deployment
instruction or a proof that the previous PCF, SMF, or UPF epoch was empty.

From WSL Ubuntu with Git, Python 3, Docker, and a running Buildx
`docker-container` builder:

```sh
python3 backend/onprem/images/pcf-boot-receipt-image/replay.py \
  --output .tmp/pcf-boot-replay-new \
  --builder cv5g-boot-receipt
```

The output directory must not exist. The script clones the exact source commit
from the official Git repository (or a supplied independent `--source-repo`),
verifies the tracked patch SHA, applies it, runs Go race tests, rebuilds the
binary, exports an OCI archive, and checks the binary, manifest, and config
against `source.lock.json`. It neither loads the image into a node nor changes
Kubernetes. Build-only host networking permits Go module download inside WSL;
there are no runtime network tests.

The reference v3 candidate was built from a Windows Git checkout. Unmodified
upstream files had CRLF endings and the seven edited files had LF endings.
Those source bytes affect Go's BuildID even when the executable code is
otherwise identical. The replay locks that line-ending layout explicitly.
The OCI manifest and config match v3 exactly; the outer OCI tar archive may
have a different SHA because its packaging metadata is generated anew.

The receipt emitted by this PCF is bound to the new Pod UID, process boot
epoch, and binary SHA256. It asserts only that this new process's UE, AF, and
BDT pools were empty before its SBI listener started. Replacing a PCF process
loses the preceding process epoch. Operators must independently fence and
verify the rest of the 5GC before any exceptional reset conclusion.
