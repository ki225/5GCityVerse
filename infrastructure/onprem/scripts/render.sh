#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<EOF
Usage: ${0##*/} [--upstream PATH] [--output NEW_DIRECTORY] [--values FILE] [--hostname NAME] [--release NAME] [--namespace NAME]
No arguments: pinned upstream cache, local single-node values and a new .tmp/onprem-render directory.
--values may repeat (last wins); supplying it replaces the default Ubuntu overlay.
--hostname overrides the default local hostname only (cannot combine with --values).
Rendering never deploys resources or connects to Kubernetes.
EOF
}

chart_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
repo_root=$(cd -- "$chart_root/../.." && pwd)
upstream='' output='' release=cityverse namespace=free5gc-onprem target_hostname=''
overlays=()
while (( $# )); do
  case "$1" in
    --upstream|--output|--values|--hostname|--release|--namespace)
      (( $# >= 2 )) || { usage >&2; exit 2; }
      [[ -n $2 && $2 != --* ]] || { usage >&2; exit 2; }
      case "$1" in
        --upstream) upstream=$2 ;;
        --output) output=$2 ;;
        --values) overlays+=(-f "$(realpath -- "$2")") ;;
        --hostname) target_hostname=$2 ;;
        --release) release=$2 ;;
        --namespace) namespace=$2 ;;
      esac
      shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done
[[ -z $output || ( ! -e $output && ! -L $output ) ]] || { printf 'Output must be a new directory\n' >&2; exit 2; }
[[ -z $target_hostname || ${#overlays[@]} -eq 0 ]] || { printf '--hostname cannot combine with --values\n' >&2; exit 2; }
[[ $namespace =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ && ${#namespace} -le 63 ]] || exit 2
for executable in helm git kubectl realpath cmp sha256sum flock; do
  command -v "$executable" >/dev/null || { printf 'Missing tool: %s\n' "$executable" >&2; exit 1; }
done
revision=$(awk '$1 == "revision:" {print $2}' "$chart_root/upstream.lock.yaml")
repository=$(awk '$1 == "repository:" {print $2}' "$chart_root/upstream.lock.yaml")
[[ $revision =~ ^[a-f0-9]{40}$ ]] || { printf 'Invalid upstream lock\n' >&2; exit 1; }
[[ $repository == https://github.com/free5gc/free5gc-helm.git ]] || { printf 'Invalid upstream repository\n' >&2; exit 1; }
stage=$(mktemp -d -t cityverse-helm.XXXXXXXX)
cache_stage=''
cleanup() {
  if [[ $stage == /tmp/cityverse-helm.* && -d $stage ]]; then rm -rf -- "$stage"; fi
  if [[ -n $cache_stage && $cache_stage == "$cache_root"/.fetch.* && -d $cache_stage ]]; then rm -rf -- "$cache_stage"; fi
}
trap cleanup EXIT
if [[ -z $upstream ]]; then
  cache_root=${XDG_CACHE_HOME:-${HOME:?HOME is required}/.cache}/5gcityverse/free5gc-helm
  mkdir -p -- "$cache_root"
  cache_root=$(realpath -- "$cache_root")
  exec {cache_lock}>"$cache_root/.fetch.lock"
  flock "$cache_lock"
  upstream=$cache_root/$revision
  if [[ ! -e $upstream && ! -L $upstream ]]; then
    cache_stage=$(mktemp -d "$cache_root/.fetch.XXXXXXXX")
    git init -q "$cache_stage"
    git -C "$cache_stage" remote add origin "$repository"
    git -C "$cache_stage" fetch --depth 1 origin "$revision" >&2
    git -C "$cache_stage" checkout --detach -q FETCH_HEAD
    mv -T -- "$cache_stage" "$upstream"
    cache_stage=''
  fi
  [[ $(git -C "$upstream" remote get-url origin) == "$repository" ]] || { printf 'Cached upstream origin mismatch\n' >&2; exit 1; }
  flock -u "$cache_lock"
fi
upstream=$(realpath -- "$upstream")
[[ $(git -C "$upstream" rev-parse HEAD) == "$revision" ]] || { printf 'Upstream revision mismatch\n' >&2; exit 1; }
[[ -z $(git -C "$upstream" status --porcelain --untracked-files=all) ]] || { printf 'Upstream checkout must be clean\n' >&2; exit 1; }

if (( ${#overlays[@]} == 0 )); then
  target_hostname=${target_hostname:-$(hostname -s)}
  [[ $target_hostname =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ && ${#target_hostname} -le 63 ]] || { printf 'Invalid local hostname; use --hostname or --values\n' >&2; exit 2; }
  sed "s/CITYVERSE_LOCAL_HOSTNAME/$target_hostname/g" "$chart_root/value.yaml" > "$stage/default-values.yaml"
  overlays=(-f "$stage/default-values.yaml")
  printf 'Single-node target hostname: %s (use --hostname if the Kubernetes node label differs)\n' "$target_hostname" >&2
fi
mkdir "$stage/chart"
cp "$chart_root/Chart.yaml" "$chart_root/values.yaml" "$chart_root/upstream.lock.yaml" "$chart_root/.helmignore" "$stage/chart/"
cp "$chart_root/values.schema.json" "$stage/chart/"
cp -R "$chart_root/templates" "$stage/chart/"
ln -s "$upstream" "$stage/upstream"
# The locked source vendors its dependencies. Do not load unrelated user Helm
# repository entries (which may have missing indexes in a fresh XDG cache).
printf 'apiVersion: v1\nrepositories: []\n' > "$stage/repositories.yaml"
mkdir "$stage/repository-cache"
HELM_REPOSITORY_CONFIG="$stage/repositories.yaml" HELM_REPOSITORY_CACHE="$stage/repository-cache" \
  helm dependency update "$stage/chart" --skip-refresh >&2
helm lint "$stage/chart" --strict "${overlays[@]}" >&2
export CITYVERSE_NAMESPACE=$namespace
for pass in first second; do
  helm template "$release" "$stage/chart" --skip-tests -n "$namespace" "${overlays[@]}" \
    --post-renderer "$chart_root/scripts/post-render.sh" > "$stage/$pass.yaml"
done
cmp "$stage/first.yaml" "$stage/second.yaml"
[[ -z $(git -C "$upstream" status --porcelain --untracked-files=all) ]] || { printf 'Upstream changed during packaging\n' >&2; exit 1; }
if [[ -z $output ]]; then
  mkdir -p -- "$repo_root/.tmp/onprem-render"
  output=$(mktemp -d "$repo_root/.tmp/onprem-render/$(date -u +%Y%m%dT%H%M%SZ).XXXXXXXX")
else
  mkdir -p -- "$(dirname -- "$output")"
  mkdir -- "$output"
fi
cp "$stage/first.yaml" "$output/runtime.yaml"
cp "$stage/chart/Chart.lock" "$output/Chart.lock"
# Preserve precisely the selected overlay order for a subsequent Helm install.
for ((i=1; i<${#overlays[@]}; i+=2)); do
  printf -v overlay_name 'values-%02d.yaml' "$(( (i+1)/2 ))"
  cp -- "${overlays[i]}" "$output/$overlay_name"
done
printf 'release: %s\nnamespace: %s\nupstreamRevision: %s\n' "$release" "$namespace" "$revision" > "$output/render-settings.yaml"
helm package "$stage/chart" --destination "$output" >&2
printf 'Rendered output: %s\n' "$(realpath -- "$output")" >&2
sha256sum "$output/runtime.yaml"
