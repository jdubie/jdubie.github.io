# The pgw agent sandbox plus Gemini CLI. Settings must be root-owned and 0644, or Gemini CLI silently skips them.
FROM node:24-bookworm-slim AS node
RUN npm i -g @google/gemini-cli@0.67.0

FROM pgw-sandbox:1
COPY --from=node /usr/local/lib/node_modules/@google/gemini-cli /usr/local/lib/node_modules/@google/gemini-cli
RUN ln -s /usr/local/lib/node_modules/@google/gemini-cli/bundle/gemini.js /usr/local/bin/gemini
COPY settings.json /etc/gemini-cli/settings.json
RUN chmod 644 /etc/gemini-cli/settings.json
