#!/usr/bin/env python3
"""
GitHub Actions Steps Fetcher - Retrieves steps from GitHub Actions jobs.
No GitHub token required for public repositories.
"""

import argparse
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime
from typing import Dict, List, Optional, Any

import requests

from xahaud_scripts.utils.clipboard import get_clipboard


class GitHubActionsFetcher:
    def __init__(self, url: str, logger: logging.Logger) -> None:
        """Initialize with GitHub repository URL."""
        self.base_url = "https://api.github.com"
        self.logger = logger

        # Parse GitHub URL to extract owner, repo, run_id, and job_id
        parsed = self.parse_github_url(url)
        self.owner = parsed["owner"]
        self.repo = parsed["repo"]
        self.run_id = parsed.get("run_id")
        self.job_id = parsed.get("job_id")

        if not self.owner or not self.repo:
            raise ValueError("Could not extract repository information from URL")

        self.logger.info(f"Repository: {self.owner}/{self.repo}")
        self.logger.info(f"Run ID: {self.run_id}, Job ID: {self.job_id}")

    def parse_github_url(self, url: str) -> Dict[str, Optional[str]]:
        """Parse GitHub URL to extract components."""
        result = {
            "owner": None,
            "repo": None,
            "run_id": None,
            "job_id": None
        }

        # Extract owner and repo
        repo_pattern = r"github\.com/([^/]+)/([^/]+)"
        repo_match = re.search(repo_pattern, url)
        if repo_match:
            result["owner"] = repo_match.group(1)
            result["repo"] = repo_match.group(2).split("/")[0]  # Remove trailing paths

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

    def make_request(self, url: str, accept_header: str = "application/vnd.github+json") -> Any:
        """Make a request to the GitHub API using requests library."""
        headers = {
            "Accept": accept_header,
            "User-Agent": "GitHubActionsStepsFetcher/1.0",
            "X-GitHub-Api-Version": "2022-11-28"
        }

        github_token = os.environ.get("GITHUB_TOKEN")
        if github_token:
            self.logger.debug("Using GitHub token from environment variable")
            headers["Authorization"] = f"Bearer {github_token}"
        else:
            self.logger.debug("No GitHub token found in environment")

        self.logger.debug(f"Making request to: {url}")
        self.logger.debug(f"Headers: {headers}")

        try:
            response = requests.get(url, headers=headers, allow_redirects=True)
            response.raise_for_status()

            content_type = response.headers.get('Content-Type', '')
            self.logger.debug(f"Response content type: {content_type}")

            if 'json' in content_type:
                self.logger.debug("Successfully parsed JSON response")
                return response.json()
            else:
                self.logger.debug(f"Received {len(response.content)} bytes of non-JSON data")
                return response.text

        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code
            if status_code in (401, 403):
                self.logger.error(f"Authentication required. GitHub API returned {status} to request {url}.")
                self.logger.error("This repository might require authentication. Try using a GitHub token.")
                self.logger.debug(f"Error Response Body: {e.response.text}")
                sys.exit(1)
            else:
                self.logger.error(f"HTTP Error: {status_code} - {e.response.reason}")
                sys.exit(1)

        except requests.exceptions.RequestException as e:
            self.logger.error(f"Request failed: {e}")
            sys.exit(1)
        except json.JSONDecodeError:
            self.logger.error("Invalid JSON response from GitHub API")
            sys.exit(1)

    def make_request_stdlib(self, url: str, accept_header: str = "application/vnd.github+json") -> Any:
        """Make a request to the GitHub API."""
        headers = {
            "Accept": accept_header,
            "X-GitHub-Api-Version": "2022-11-28"
        }

        # Check for GitHub token in environment
        github_token = os.environ.get("GITHUB_TOKEN")
        if github_token:
            self.logger.debug("Using GitHub token from environment variable")
            headers["Authorization"] = f"Bearer {github_token}"
        else:
            self.logger.debug("No GitHub token found in environment")

        self.logger.debug(f"Making request to: {url}")
        stripped_headers = {k: v for k, v in headers.items() if k != "Authorization"}
        if "Authorization" in headers:
            auth_header = headers["Authorization"]
            stripped_headers["Authorization"] = auth_header[:5] + "..." + auth_header[-5:]
        self.logger.debug(f"Headers: {headers}")

        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req) as response:
                content_type = response.getheader('Content-Type', '')
                self.logger.debug(f"Response content type: {content_type}")

                if 'json' in content_type:
                    data = json.loads(response.read().decode('utf-8'))
                    self.logger.debug("Successfully parsed JSON response")
                    return data
                else:
                    data = response.read().decode('utf-8')
                    self.logger.debug(f"Received {len(data)} bytes of non-JSON data")
                    return data
        except urllib.error.HTTPError as e:
            if e.code == 401 or e.code == 403:
                self.logger.error(f"Authentication required. GitHub API returned {e.code} to request {url}.")
                self.logger.error("This repository might require authentication. Try using a GitHub token.")
                try:
                    self.logger.debug("Error Response Body:" + e.read().decode())  # Print the error response body
                except Exception as inner_e:
                    self.logger.error(f"Error reading response body: {inner_e}")
                sys.exit(1)
            else:
                self.logger.error(f"HTTP Error: {e.code} - {e.reason}")
                sys.exit(1)
        except urllib.error.URLError as e:
            self.logger.error(f"URL Error: {e.reason}")
            sys.exit(1)
        except json.JSONDecodeError:
            self.logger.error("Invalid JSON response from GitHub API")
            sys.exit(1)

    def get_run_jobs(self, run_id: str) -> List[Dict[str, Any]]:
        """Get all jobs for a specific workflow run."""
        url = f"{self.base_url}/repos/{self.owner}/{self.repo}/actions/runs/{run_id}/jobs"
        self.logger.info(f"Fetching jobs for run ID: {run_id}")
        response = self.make_request(url)
        jobs = response.get("jobs", [])
        self.logger.info(f"Found {len(jobs)} jobs")
        return jobs

    def get_job_details(self, job_id: str) -> Dict[str, Any]:
        """Get details for a specific job."""
        url = f"{self.base_url}/repos/{self.owner}/{self.repo}/actions/jobs/{job_id}"
        self.logger.info(f"Fetching details for job ID: {job_id}")
        return self.make_request(url)

    def get_job_logs(self, job_id: str) -> str:
        """Get logs for a specific job."""
        # Note: This endpoint returns raw logs, not JSON
        url = f"{self.base_url}/repos/{self.owner}/{self.repo}/actions/jobs/{job_id}/logs"
        self.logger.info(f"Fetching logs for job ID: {job_id}")
        try:
            # Use different accept header for logs
            logs = self.make_request(url)
            self.logger.info(f"Retrieved {len(logs)} bytes of log data")
            return logs
        except Exception as e:
            self.logger.warning(f"Could not fetch logs for job {job_id}: {str(e)}")
            return ""

    def extract_step_logs(self, full_log: str) -> Dict[str, str]:
        """Extract individual step logs from the full job log."""
        step_logs = {}
        current_step = None
        current_content = []

        # Pattern to detect step headers in GitHub Actions logs
        step_pattern = re.compile(r"##\[group\](.*?)(Starting|Finishing|Completing|Running|Executing): (.*?)$")

        self.logger.debug("Extracting step logs from full job log")
        line_count = 0

        for line in full_log.splitlines():
            line_count += 1
            match = step_pattern.search(line)
            if match:
                # If we were collecting logs for a previous step, save them
                if current_step:
                    step_logs[current_step] = "\n".join(current_content)
                    self.logger.debug(f"Extracted {len(current_content)} lines for step: {current_step}")
                    current_content = []

                action = match.group(2)
                step_name = match.group(3).strip()

                if action == "Starting":
                    current_step = step_name
                    self.logger.debug(f"Found step: {current_step}")
                elif current_step is None and action in ["Running", "Executing"]:
                    current_step = step_name
                    self.logger.debug(f"Found in-progress step: {current_step}")
            elif current_step:
                current_content.append(line)

        # Save the last step's content
        if current_step and current_content:
            step_logs[current_step] = "\n".join(current_content)
            self.logger.debug(f"Extracted {len(current_content)} lines for step: {current_step}")

        self.logger.info(f"Processed {line_count} lines of logs, extracted {len(step_logs)} steps")
        return step_logs

    def format_duration(self, start_time: str, end_time: str) -> str:
        """Format duration between two ISO timestamps."""
        if not start_time or not end_time:
            return "N/A"

        try:
            start = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            end = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
            duration = (end - start).total_seconds()

            if duration < 60:
                return f"{duration:.2f} seconds"
            elif duration < 3600:
                minutes = int(duration // 60)
                seconds = int(duration % 60)
                return f"{minutes}m {seconds}s"
            else:
                hours = int(duration // 3600)
                minutes = int((duration % 3600) // 60)
                return f"{hours}h {minutes}m"
        except (ValueError, TypeError) as e:
            self.logger.debug(f"Error calculating duration: {e}")
            return "N/A"

    def print_steps(self, job: Dict[str, Any], include_logs: bool = True) -> None:
        """Print steps information for a job."""
        job_name = job.get("name", "Unknown Job")
        steps = job.get("steps", [])

        print(f"\nJob: {job_name} (ID: {job['id']})")
        print("=" * 80)

        if not steps:
            print("No steps found for this job.")
            return

        # Fetch logs if requested
        job_logs = ""
        step_logs = {}
        if include_logs:
            job_logs = self.get_job_logs(str(job['id']))
            if job_logs:
                step_logs = self.extract_step_logs(job_logs)

        for i, step in enumerate(steps, 1):
            step_name = step.get("name", f"Step {i}")
            step_number = step.get("number", i)
            status = step.get("conclusion", step.get("status", "unknown"))

            # Format duration
            duration_str = self.format_duration(
                step.get("started_at"),
                step.get("completed_at")
            )

            print(f"\nStep {step_number}: {step_name}")
            print(f"  Status: {status}")
            print(f"  Duration: {duration_str}")

            # Print log output if available
            if step_name in step_logs and step_logs[step_name].strip():
                print("\n  Output:")
                print("-" * 80)
                # Limit output to avoid excessive prints
                log_lines = step_logs[step_name].splitlines()
                if len(log_lines) > 50:
                    print("\n".join(log_lines[:20]))
                    print(f"\n... {len(log_lines) - 40} more lines ...\n")
                    print("\n".join(log_lines[-20:]))
                else:
                    print(step_logs[step_name])
                print("-" * 80)


def setup_logging(level_name: str) -> logging.Logger:
    """Set up logging with the specified level."""
    level_map = {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
        "critical": logging.CRITICAL
    }

    # Default to ERROR if level is not recognized
    level = level_map.get(level_name.lower(), logging.ERROR)

    # Configure logger
    logger = logging.getLogger("github_actions_fetcher")
    logger.setLevel(level)

    # Create console handler with formatting
    handler = logging.StreamHandler()
    handler.setLevel(level)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)

    # Add handler to logger
    logger.addHandler(handler)

    return logger


def parse_arguments() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Fetch GitHub Actions job steps")
    parser.add_argument("url", help="GitHub Actions URL (run or job URL)")
    parser.add_argument("--job", type=str, help="Job ID to filter (optional)")
    parser.add_argument("--no-logs", action="store_true", help="Don't fetch step logs")
    parser.add_argument(
        "--log-level",
        choices=["debug", "info", "warning", "error", "critical"],
        default="error",
        help="Set logging level (default: error)"
    )
    parser.add_argument("--raw-logs", action="store_true", help="Output raw logs without parsing")
    parser.add_argument("--token", help="GitHub API token (overrides GITHUB_TOKEN environment variable)")
    return parser.parse_args()


def main() -> None:
    """Main function."""
    args = parse_arguments()
    if args.url == '<clip>':
        args.url = get_clipboard()

    # Set up logging
    logger = setup_logging(args.log_level)

    try:
        logger.info(f"Starting GitHub Actions Steps Fetcher with URL: {args.url}")
        fetcher = GitHubActionsFetcher(args.url, logger)

        if args.token:
            os.environ["GITHUB_TOKEN"] = args.token
            if args.token == "none":
                os.environ.pop("GITHUB_TOKEN", None)

        # Override job_id if provided via command line
        if args.job:
            fetcher.job_id = args.job
            logger.info(f"Using job ID from command line: {args.job}")

        # If we have a job_id, fetch single job details
        if fetcher.job_id:
            job = fetcher.get_job_details(fetcher.job_id)
            if args.raw_logs:
                logs = fetcher.get_job_logs(fetcher.job_id)
                print(logs)
                sys.exit(0)
            fetcher.print_steps(job, not args.no_logs)

        # If we only have a run_id, fetch all jobs for that run
        elif fetcher.run_id:
            jobs = fetcher.get_run_jobs(fetcher.run_id)
            if not jobs:
                logger.error("No jobs found for this workflow run.")
                sys.exit(1)

            for job in jobs:
                # For each job in the run, get detailed information
                detailed_job = fetcher.get_job_details(str(job["id"]))
                fetcher.print_steps(detailed_job, not args.no_logs)
        else:
            logger.error("Could not extract run ID or job ID from URL")
            sys.exit(1)

    except ValueError as e:
        logger.error(f"{str(e)}")
        sys.exit(1)
    except Exception as e:
        logger.critical(f"Unexpected error: {str(e)}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
