# dashboard/ — Stream B (sole owner of all UI)

React + Vite. Shows the session timeline, held actions, the rewind dialog (dry run, then run), live rewind progress, checksum proof and Sentry trace links.
It talks to the gateway only through `contracts/openapi.yaml` and the SSE events. It uses Prism mocks until the gateway is ready. The design is done in Paper.
