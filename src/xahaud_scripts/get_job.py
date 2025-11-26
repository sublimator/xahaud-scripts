#!/usr/bin/env python3
"""
GitHub Actions Steps Fetcher - Retrieves steps from GitHub Actions jobs.
No GitHub token required for public repositories.
"""

import os
import re
import sys
from datetime import datetime
from typing import Any

import click
import requests

from xahaud_scripts.utils.clipboard import get_clipboard
from xahaud_scripts.utils.logging import make_logger, setup_logging

logger = make_logger(__name__)


class GitHubActionsFetcher:
    def __init__(self, url: str) -> None:
        """Initialize with GitHub repository URL."""
        self.base_url = "https://api.github.com"

        # Parse GitHub URL to extract owner, repo, run_id, and job_id
        parsed = self._parse_github_url(url)
        self.owner = parsed["owner"]
        self.repo = parsed["repo"]
        self.run_id = parsed.get("run_id")
        self.job_id = parsed.get("job_id")

        if not self.owner or not self.repo:
            raise ValueError("Could not extract repository information from URL")

        logger.info(f"Repository: {self.owner}/{self.repo}")
        logger.info(f"Run ID: {self.run_id}, Job ID: {self.job_id}")

    def _parse_github_url(self, url: str) -> dict[str, str | None]:
        """Parse GitHub URL to extract components."""
        result: dict[str, str | None] = {
            "owner": None,
            "repo": None,
            "run_id": None,
            "job_id": None,
        }

        # Extract owner and repo
        repo_pattern = r"github\.com/([^/]+)/([^/]+)"
        repo_match = re.search(repo_pattern, url)
        if repo_match:
            result["owner"] = repo_match.group(1)
            result["repo"] = repo_match.group(2).split("/")[0]

        # Extract run ID
        run_pattern = r"runs/(\d+)"
        run_match = re.search(run_pattern, url)
        if run_match:
            result["run_id"] = run_match.group(1)

        # Extract job ID
        job_pattern = r"job/(\d+)"
        job_match = re.search(job_pattern, url)
        if job_match:
            result["job_id"] = job_match.group(1)

        return result

    def _make_request(
        self, url: str, accept_header: str = "application/vnd.github+json"
    ) -> Any:
        """Make a request to the GitHub API."""
        headers = {
            "Accept": accept_header,
            "User-Agent": "GitHubActionsStepsFetcher/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        github_token = os.environ.get("GITHUB_TOKEN")
        if github_token:
            logger.debug("Using GitHub token from environment variable")
            headers["Authorization"] = f"Bearer {github_token}"
        else:
            logger.debug("No GitHub token found in environment")

        logger.debug(f"Making request to: {url}")

        try:
            response = requests.get(url, headers=headers, allow_redirects=True)
            response.raise_for_status()

            content_type = response.headers.get("Content-Type", "")
            logger.debug(f"Response content type: {content_type}")

            if "json" in content_type:
                logger.debug("Successfully parsed JSON response")
                return response.json()
            else:
                logger.debug(f"Received {len(response.content)} bytes of non-JSON data")
                return response.text

        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code if e.response else 0
            if status_code in (401, 403):
                logger.error(
                    f"Authentication required. GitHub API returned {status_code}."
                )
                logger.error(
                    "This repository might require authentication. "
                    "Set GITHUB_TOKEN or use --token."
                )
                sys.exit(1)
            else:
                reason = e.response.reason if e.response else "Unknown"
                logger.error(f"HTTP Error: {status_code} - {reason}")
                sys.exit(1)

        except requests.exceptions.RequestException as e:
            logger.error(f"Request failed: {e}")
            sys.exit(1)

    def get_run_jobs(self, run_id: str) -> list[dict[str, Any]]:
        """Get all jobs for a specific workflow run."""
        url = f"{self.base_url}/repos/{self.owner}/{self.repo}/actions/runs/{run_id}/jobs"
        logger.info(f"Fetching jobs for run ID: {run_id}")
        response = self._make_request(url)
        jobs = response.get("jobs", [])
        logger.info(f"Found {len(jobs)} jobs")
        return jobs

    def get_job_details(self, job_id: str) -> dict[str, Any]:
        """Get details for a specific job."""
        url = f"{self.base_url}/repos/{self.owner}/{self.repo}/actions/jobs/{job_id}"
        logger.info(f"Fetching details for job ID: {job_id}")
        return self._make_request(url)

    def get_job_logs(self, job_id: str) -> str:
        """Get logs for a specific job."""
        url = f"{self.base_url}/repos/{self.owner}/{self.repo}/actions/jobs/{job_id}/logs"
        logger.info(f"Fetching logs for job ID: {job_id}")
        try:
            logs = self._make_request(url)
            logger.info(f"Retrieved {len(logs)} bytes of log data")
            return logs
        except Exception as e:
            logger.warning(f"Could not fetch logs for job {job_id}: {e}")
            return ""

    def _extract_step_logs(self, full_log: str) -> dict[str, str]:
        """Extract individual step logs from the full job log."""
        step_logs: dict[str, str] = {}
        current_step: str | None = None
        current_content: list[str] = []

        step_pattern = re.compile(
            r"##\[group\](.*?)(Starting|Finishing|Completing|Running|Executing): (.*?)$"
        )

        logger.debug("Extracting step logs from full job log")
        line_count = 0

        for line in full_log.splitlines():
            line_count += 1
            match = step_pattern.search(line)
            if match:
                if current_step:
                    step_logs[current_step] = "\n".join(current_content)
                    logger.debug(
                        f"Extracted {len(current_content)} lines for step: {current_step}"
                    )
                    current_content = []

                action = match.group(2)
                step_name = match.group(3).strip()

                if action == "Starting":
                    current_step = step_name
                    logger.debug(f"Found step: {current_step}")
                elif current_step is None and action in ["Running", "Executing"]:
                    current_step = step_name
                    logger.debug(f"Found in-progress step: {current_step}")
            elif current_step:
                current_content.append(line)

        if current_step and current_content:
            step_logs[current_step] = "\n".join(current_content)
            logger.debug(
                f"Extracted {len(current_content)} lines for step: {current_step}"
            )

        logger.info(
            f"Processed {line_count} lines of logs, extracted {len(step_logs)} steps"
        )
        return step_logs

    def _format_duration(self, start_time: str | None, end_time: str | None) -> str:
        """Format duration between two ISO timestamps."""
        if not start_time or not end_time:
            return "N/A"

        try:
            start = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            end = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
            duration = (end - start).total_seconds()

            if duration < 60:
                return f"{duration:.2f}s"
            elif duration < 3600:
                minutes = int(duration // 60)
                seconds = int(duration % 60)
                return f"{minutes}m {seconds}s"
            else:
                hours = int(duration // 3600)
                minutes = int((duration % 3600) // 60)
                return f"{hours}h {minutes}m"
        except (ValueError, TypeError) as e:
            logger.debug(f"Error calculating duration: {e}")
            return "N/A"

    def print_steps(self, job: dict[str, Any], include_logs: bool = True) -> None:
        """Print steps information for a job."""
        job_name = job.get("name", "Unknown Job")
        steps = job.get("steps", [])

        print(f"\nJob: {job_name} (ID: {job['id']})")
        print("=" * 80)

        if not steps:
            print("No steps found for this job.")
            return

        job_logs = ""
        step_logs: dict[str, str] = {}
        if include_logs:
            job_logs = self.get_job_logs(str(job["id"]))
            if job_logs:
                step_logs = self._extract_step_logs(job_logs)

        for i, step in enumerate(steps, 1):
            step_name = step.get("name", f"Step {i}")
            step_number = step.get("number", i)
            status = step.get("conclusion", step.get("status", "unknown"))

            duration_str = self._format_duration(
                step.get("started_at"), step.get("completed_at")
            )

            print(f"\nStep {step_number}: {step_name}")
            print(f"  Status: {status}")
            print(f"  Duration: {duration_str}")

            if step_name in step_logs and step_logs[step_name].strip():
                print("\n  Output:")
                print("-" * 80)
                log_lines = step_logs[step_name].splitlines()
                if len(log_lines) > 50:
                    print("\n".join(log_lines[:20]))
                    print(f"\n... {len(log_lines) - 40} more lines ...\n")
                    print("\n".join(log_lines[-20:]))
                else:
                    print(step_logs[step_name])
                print("-" * 80)


@click.command()
@click.argument("url")
@click.option("--job", "job_id", help="Job ID to filter (optional)")
@click.option("--no-logs", is_flag=True, help="Don't fetch step logs")
@click.option(
    "--log-level",
    type=click.Choice(["debug", "info", "warning", "error"], case_sensitive=False),
    default="error",
    help="Set logging level",
)
@click.option("--raw-logs", is_flag=True, help="Output raw logs without parsing")
@click.option(
    "--token",
    envvar="GITHUB_TOKEN",
    help="GitHub API token (or set GITHUB_TOKEN env var)",
)
def main(
    url: str,
    job_id: str | None,
    no_logs: bool,
    log_level: str,
    raw_logs: bool,
    token: str | None,
) -> None:
    """Fetch GitHub Actions job steps.

    URL can be a GitHub Actions run or job URL, or "<clip>" to use clipboard.
    """
    if url == "<clip>":
        url = get_clipboard()

    setup_logging(log_level, logger)

    if token:
        os.environ["GITHUB_TOKEN"] = token

    try:
        logger.info(f"Starting GitHub Actions Steps Fetcher with URL: {url}")
        fetcher = GitHubActionsFetcher(url)

        if job_id:
            fetcher.job_id = job_id
            logger.info(f"Using job ID from command line: {job_id}")

        if fetcher.job_id:
            job = fetcher.get_job_details(fetcher.job_id)
            if raw_logs:
                logs = fetcher.get_job_logs(fetcher.job_id)
                print(logs)
                return
            fetcher.print_steps(job, not no_logs)

        elif fetcher.run_id:
            jobs = fetcher.get_run_jobs(fetcher.run_id)
            if not jobs:
                logger.error("No jobs found for this workflow run.")
                sys.exit(1)

            for job in jobs:
                detailed_job = fetcher.get_job_details(str(job["id"]))
                fetcher.print_steps(detailed_job, not no_logs)
        else:
            logger.error("Could not extract run ID or job ID from URL")
            sys.exit(1)

    except ValueError as e:
        logger.error(str(e))
        sys.exit(1)
    except Exception as e:
        logger.critical(f"Unexpected error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
