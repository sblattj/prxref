#!/usr/bin/env bash
# Build the gate image, push it to ECR, and deploy the stack with that image's digest.
#
# Usage: deploy/aws/deploy.sh [--profile NAME] [extra sam deploy args...]
#
# Reads the saved samconfig.toml next to this script (image_repository and the parameter
# overrides from the first `sam deploy --save-params`) and adds ImageUri. The image is built
# with buildx and Docker v2 media types: `sam build` under the containerd image store
# (OrbStack, Docker Desktop) pushes a manifest with mixed layer types, and Lambda fails such an
# image at init with Runtime.InvalidEntrypoint.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
profile=${AWS_PROFILE:-prxref-deploy}
if [[ ${1:-} == --profile ]]; then profile=$2; shift 2; fi

read_config() {
  python3 - "$here/samconfig.toml" <<'PY'
import sys, tomllib
cfg = tomllib.load(open(sys.argv[1], "rb"))["default"]["deploy"]["parameters"]
print(cfg["image_repository"])
for key, value in cfg.get("parameter_overrides", {}).items():
    if key != "ImageUri":
        print(f"{key}={value}")
PY
}
[[ -f $here/samconfig.toml ]] || { echo "no samconfig.toml: run the first deploy from README.md" >&2; exit 2; }
config=()
while IFS= read -r line; do config+=("$line"); done < <(read_config)
repo=${config[0]}
overrides=("${config[@]:1}")
region=$(aws configure get region --profile "$profile" || true)
region=${region:-us-west-2}

tag="gate-$(git -C "$root" rev-parse --short=12 HEAD)"
if [[ -n $(git -C "$root" status --porcelain -- src pyproject.toml deploy/aws/Dockerfile) ]]; then
  tag="$tag-dirty-$(date +%s)"
fi

aws ecr get-login-password --profile "$profile" --region "$region" \
  | docker login --username AWS --password-stdin "${repo%%/*}" >/dev/null
docker buildx build --platform linux/amd64 --provenance=false --sbom=false \
  -f "$here/Dockerfile" \
  --output "type=image,name=$repo:$tag,push=true,oci-mediatypes=false,compression=gzip,force-compression=true" \
  "$root"

digest=$(aws ecr describe-images --repository-name "${repo#*/}" --image-ids "imageTag=$tag" \
  --profile "$profile" --region "$region" --query 'imageDetails[0].imageDigest' --output text)
echo "image: $repo@$digest"

cd "$here"
sam deploy --profile "$profile" --template-file template.yaml \
  --parameter-overrides "${overrides[@]}" "ImageUri=$repo@$digest" "$@"
