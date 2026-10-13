from core import tools as toolreg


_JSON_SCHEMA_TYPES = {
    "array",
    "boolean",
    "integer",
    "null",
    "number",
    "object",
    "string",
}


def _validate_property(schema, path):
    assert isinstance(schema, dict), f"{path} muss ein Schema-Objekt sein"
    schema_type = schema.get("type")
    types = schema_type if isinstance(schema_type, list) else [schema_type]
    assert types and all(item in _JSON_SCHEMA_TYPES for item in types), (
        f"{path} hat ungültigen oder fehlenden type: {schema_type!r}"
    )

    if "object" in types:
        properties = schema.get("properties", {})
        assert isinstance(properties, dict), f"{path}.properties muss ein Objekt sein"
        required = schema.get("required", [])
        assert isinstance(required, list), f"{path}.required muss eine Liste sein"
        assert set(required) <= set(properties), f"{path}.required enthält unbekannte Properties"
        for name, child in properties.items():
            _validate_property(child, f"{path}.properties.{name}")

    if "array" in types:
        assert "items" in schema, f"{path} ist ein Array ohne items"
        _validate_property(schema["items"], f"{path}.items")


def test_every_registered_tool_exports_a_valid_openai_schema():
    import core.skills  # noqa: F401  Registriert alle produktiven Agent-Tools.

    schemas = toolreg.ollama_schemas()

    assert schemas
    for tool in schemas:
        function = tool["function"]
        parameters = function["parameters"]
        assert parameters.get("type") == "object", function["name"]
        assert isinstance(parameters.get("properties"), dict), function["name"]
        _validate_property(parameters, function["name"])


def test_git_history_import_has_object_properties():
    import core.skills  # noqa: F401

    schema = next(
        tool["function"]["parameters"]
        for tool in toolreg.ollama_schemas()
        if tool["function"]["name"] == "git_history_import"
    )

    assert schema["properties"]["repos"]["type"] == "string"
    assert schema["properties"]["limit"]["type"] == "integer"
