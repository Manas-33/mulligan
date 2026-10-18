"""The worker agent: runs the Guardian's tasks through Mulligan's MCP tools as the `fetch` agent.

uv run python -m mulligan_fetch.worker
"""

from __future__ import annotations

from uagents import Agent, Context

from mulligan_fetch.config import Config
from mulligan_fetch.messages import TaskRequest, TaskResult
from mulligan_fetch.tasks import run_through_mulligan

config = Config.from_env()
config.check_seeds()

worker = Agent(
    name="mulligan-worker",
    seed=config.worker_seed,
    port=config.worker_port,
    endpoint=[f"http://127.0.0.1:{config.worker_port}/submit"],
    description="Runs tasks for Mulligan Guardian, only through the Mulligan gateway.",
)


@worker.on_message(model=TaskRequest, replies=TaskResult)
async def handle_task(ctx: Context, sender: str, msg: TaskRequest) -> None:
    ctx.logger.info(f"task {msg.request_id} from {sender}: {msg.task!r}")
    try:
        run = await run_through_mulligan(config.mcp_url, config.fetch_token, msg.task)
        result = TaskResult(
            request_id=msg.request_id,
            task=msg.task,
            steps=run.as_dicts(),
            action_ids=run.action_ids,
        )
    except Exception as exc:  # noqa: BLE001 (reported back to the Guardian)
        result = TaskResult(
            request_id=msg.request_id, task=msg.task, steps=[], action_ids=[], error=str(exc)[:300]
        )
    await ctx.send(sender, result)


if __name__ == "__main__":
    worker.run()
