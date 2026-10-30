from backend.onprem.operations.deployment import policy_callback


def deployment(*, fenced):
    docs = policy_callback.objects("10.244.1.22", recovery_fence=fenced)
    return next(doc for doc in docs if doc["kind"] == "Deployment")


def test_callback_fence_renderer_explicit_opt_in_only():
    default_docs = {d["kind"]: d for d in policy_callback.objects("10.244.1.22")}
    selected_docs = {d["kind"]: d for d in policy_callback.objects("10.244.1.22", recovery_fence=True)}
    default = default_docs["Deployment"]
    selected = selected_docs["Deployment"]
    base = default["spec"]["template"]["spec"]["containers"][0]
    candidate = selected["spec"]["template"]["spec"]["containers"][0]
    assert not any(row["name"].startswith("CITYVERSE_AF_RECOVERY") for row in base["env"])
    assert base["readinessProbe"] == {"httpGet": {"path": "/health", "port": "http"},
                                      "periodSeconds": 5}
    env = {row["name"]: row for row in candidate["env"]}
    assert env["CITYVERSE_AF_RECOVERY_FENCE"]["value"] == "1"
    assert env["CITYVERSE_AF_POD_UID"]["valueFrom"]["fieldRef"]["fieldPath"] == "metadata.uid"
    assert candidate["readinessProbe"]["exec"]["command"][0] == "python"
    assert "127.0.0.1:9082/internal/recovery-fence" in candidate["readinessProbe"]["exec"]["command"][2]
    assert candidate["image"] == base["image"]
    assert selected["spec"]["strategy"] == {"type": "Recreate"}
    assert default_docs["ConfigMap"]["metadata"]["name"] == "cityverse-af-callback-source"
    source = selected_docs["ConfigMap"]
    assert source["immutable"] is True
    assert source["metadata"]["name"].startswith("cityverse-af-callback-source-gen263-")
    assert selected["spec"]["template"]["spec"]["volumes"][0]["configMap"]["name"] == source["metadata"]["name"]
    assert default["spec"]["template"]["spec"]["volumes"][0]["configMap"]["name"] == "cityverse-af-callback-source"


def test_callback_fence_renderer_rejects_ambiguous_opt_in():
    for value in (None, "1", 1):
        try:
            policy_callback.objects("10.244.1.22", recovery_fence=value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Accepted ambiguous recovery mode: {value!r}")
