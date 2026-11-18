from memnc.verifier import LearnedVerifier


def train_on_corpus(verifier: LearnedVerifier, corpus: list):
    """Helper to run training."""
    verifier.train(corpus)
    return verifier.weights


if __name__ == "__main__":
    # Test data creation
    print("Starting Verifier V1 Training...")

    # 1. Real code (Positive)
    real_code = {
        "src/memnc/api.py": "import asyncio\nimport logging\n\nasync def hello():\n    print('hello world')\n    return True\n",
        "README.md": "# MemNC\nDocumentation for the mesh.\n",
    }

    # 2. KugelAudio-style fabrication (Negative)
    # Report claiming high quality/metrics but actually just text
    fabrication = {
        "report.md": "# Analysis Result\n- Total artifacts: 100\n- Success: 100%\n- Speed: 500 tokens/s\n- Error rate: 0%\n",
        "metrics.json": '{ "status": "complete" }',
    }

    # 3. Stubs (Negative)
    stubs = {
        "stub.py": "def task():\n    pass\n\n# TODO: implement logic\n# FIXME: bug\n",
        "empty.txt": "",
    }

    corpus = [(real_code, 1), (stubs, 0), (fabrication, 0)]

    verifier = LearnedVerifier()
    verifier.train(corpus)

    print("Testing scores...")
    score_real = verifier.verify(real_code)
    score_fab = verifier.verify(fabrication)
    score_stub = verifier.verify(stubs)
    score_empty = verifier.verify({})

    print(f"Real score: {score_real:.4f}")
    print(f"Fab score: {score_fab:.4f}")
    print(f"Stub score: {score_stub:.4f}")
    print(f"Empty score: {score_empty:.4f}")

    # Weights check
    weight_file = "src/memnc/verifier_weights.json"
    verifier.save_weights(weight_file)
    print(f"Weights saved to {weight_file}")
