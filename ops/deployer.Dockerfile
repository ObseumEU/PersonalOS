# The deployer (pos.selfdeploy): git + docker compose + the PersonalOS backend package.
# Node comes from the official image: Alpine's nodejs package is built without
# TypeScript stripping (ERR_NO_TYPESCRIPT), which the web tests (npm test) need.
FROM node:22-alpine AS node

FROM docker:27-cli
RUN apk add --no-cache python3 py3-pip git bash libstdc++ libgcc \
    && git config --global --add safe.directory "*"
# node and npm for the web build and the web tests.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && node -e "process.exit(process.features.typescript ? 0 : 1)"
WORKDIR /app
COPY backend/pyproject.toml ./
COPY backend/src ./src
RUN python3 -m venv /venv && /venv/bin/pip install --no-cache-dir .
ENV PATH="/venv/bin:$PATH"
ENTRYPOINT []
