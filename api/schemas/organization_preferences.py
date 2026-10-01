from pydantic import BaseModel


class OrganizationPreferences(BaseModel):
    test_phone_number: str | None = None
    timezone: str | None = None
    # Whether Custom Tools (the function editor) shows in this org's
    # navigation. Unset means hidden. Hiding it only removes it from view:
    # tools already built with it keep running on calls.
    show_custom_tools: bool | None = None
