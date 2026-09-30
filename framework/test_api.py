"""
Simple test to verify DeepSeek API connectivity and basic functionality
"""

import logging
import os
import sys

import pytest

from agents.base_agent import BaseAgent

# Setup logging
logging.basicConfig(level=logging.INFO)


def test_deepseek_api():
    """Test DeepSeek API connectivity."""
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        pytest.skip("set DEEPSEEK_API_KEY to run the live integration test")
    print("🧪 Testing DeepSeek API connectivity...")

    config = {
        "api_key": api_key,
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "exploration_strategy": "random",
        "max_test_cases": 5,
    }

    agent = BaseAgent(config)

    # Simple test prompt
    test_prompt = "Generate a simple SQL SELECT query for a table called users with columns id, name, and age."

    print(f"📝 Sending test prompt: {test_prompt}")

    response = agent.call_llm(test_prompt, temperature=0.7)

    if response:
        print("✅ API call successful!")
        print(f"📄 Response: {response[:200]}...")
        print(f"📊 Metrics: {agent.get_metrics()}")
        return True
    else:
        print("❌ API call failed!")
        return False


if __name__ == "__main__":
    success = test_deepseek_api()
    sys.exit(0 if success else 1)
