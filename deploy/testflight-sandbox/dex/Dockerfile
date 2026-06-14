# Minimal Dex image for the Family Assistant sandbox: the official Dex image
# plus our baked-in config. All deployment-specific values are supplied at
# runtime via environment variables (see config.yaml), so this image contains
# no secrets and is safe to rebuild anywhere.
#
# Build context for this image is deploy/testflight-sandbox/dex (see render.yaml
# / docker-compose.yaml), so config.yaml is referenced relative to that dir.
FROM ghcr.io/dexidp/dex:v2.41.1

COPY config.yaml /etc/dex/config.yaml

# The base image's entrypoint is the dex binary wrapper; we only override the
# config path it serves.
CMD ["dex", "serve", "/etc/dex/config.yaml"]
