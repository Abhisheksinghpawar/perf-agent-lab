import unittest

from agent.skills.app_env import build_hint

REAL_TAIL = """[ERROR] Failed to execute goal org.apache.maven.plugins:maven-enforcer-plugin:3.6.3:enforce (enforce-java) on project jpetstore:
[ERROR] Rule 1: org.apache.maven.enforcer.rules.version.RequireJavaVersion failed with message:
[ERROR] Detected JDK /usr/lib/jvm/temurin-17-jdk-amd64 is version 17.0.20-1 which is not in the allowed range [21,22),[25,26),[26,27),[27,28).
"""


class BuildHintTests(unittest.TestCase):
    def test_wrong_jdk_is_explained_with_versions(self):
        hint = build_hint(REAL_TAIL)           # real output captured from the first GitHub Actions run
        self.assertIn("17.0.20-1", hint)
        self.assertIn("[21,22)", hint)
        self.assertIn("Install a matching JDK", hint)

    def test_network_and_cache_problems(self):
        self.assertIn("network", build_hint("Could not transfer artifact org.x:y:jar from central"))
        self.assertIn("PERF_MAVEN_REPO", build_hint("java.nio.file.AccessDeniedException: C:\\\\Users\\\\x\\\\.m2\\\\repository\\\\a.pom.lastUpdated"))

    def test_unknown_failure_has_a_generic_pointer(self):
        self.assertIn("Maven output", build_hint("something else entirely"))


if __name__ == "__main__":
    unittest.main()
