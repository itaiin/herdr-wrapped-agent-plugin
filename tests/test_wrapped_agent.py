import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wrapped_agent import DEFAULT_AGENTS, identify_agent, match_execs, parse_exec_container  # noqa: E402


class ParseExecContainer(unittest.TestCase):
    def test_wrapper_argv(self):
        argv = ["/usr/bin/docker", "exec", "--interactive", "--tty", "--workdir", "/home/u", "--user", "u",
                "--env", "LANG=C.UTF-8", "--env", "TERM=xterm", "dev-1", "/bin/bash", "-l"]
        self.assertEqual(parse_exec_container(argv), "dev-1")

    def test_short_clusters_and_equals(self):
        self.assertEqual(parse_exec_container(["docker", "exec", "-it", "c1", "bash"]), "c1")
        self.assertEqual(parse_exec_container(["docker", "exec", "-itu", "root", "c1"]), "c1")
        self.assertEqual(parse_exec_container(["docker", "exec", "-e", "A=1", "-it", "c1"]), "c1")
        self.assertEqual(parse_exec_container(["docker", "exec", "--env=A=1", "--user=root", "c1"]), "c1")
        self.assertEqual(parse_exec_container(["docker", "exec", "-uroot", "c1"]), "c1")

    def test_global_opts_and_container_subcommand(self):
        self.assertEqual(parse_exec_container(["docker", "--context", "x", "container", "exec", "-it", "c1"]), "c1")

    def test_not_exec(self):
        self.assertIsNone(parse_exec_container(["docker", "run", "-it", "img"]))
        self.assertIsNone(parse_exec_container(["bash"]))
        self.assertIsNone(parse_exec_container([]))


class MatchExecs(unittest.TestCase):
    def test_pairs_by_start_time(self):
        clients = [(10, 100), (20, 200)]
        execs = [(11, 101), (21, 202), (5, 50)]
        self.assertEqual(match_execs(clients, execs), {10: 11, 20: 21})

    def test_skips_taken(self):
        self.assertEqual(match_execs([(20, 200)], [(11, 101), (21, 202)], taken=[21]), {})

    def test_exec_before_client_never_matches(self):
        self.assertEqual(match_execs([(20, 200)], [(11, 150)]), {})


class IdentifyAgent(unittest.TestCase):
    def test_native_binary(self):
        self.assertEqual(identify_agent("claude", ["claude"], None, DEFAULT_AGENTS), "claude")

    def test_node_script(self):
        argv = ["node", "--no-warnings", "/usr/lib/node_modules/@openai/codex/bin/codex.js"]
        self.assertEqual(identify_agent("node", argv, None, DEFAULT_AGENTS), "codex")

    def test_env_hint_wins(self):
        self.assertEqual(identify_agent("fence", ["fence"], "claude", DEFAULT_AGENTS), "claude")

    def test_unknown(self):
        self.assertIsNone(identify_agent("vim", ["vim", "x"], None, DEFAULT_AGENTS))


if __name__ == "__main__":
    unittest.main()
