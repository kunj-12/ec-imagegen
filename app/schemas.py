from pydantic import BaseModel


class CreateRestyleBatchRequest(BaseModel):
    extra_styling: str | None = None
    # the photo itself arrives as multipart UploadFile in the router, not here


class JobOut(BaseModel):
    model_config = {"protected_namespaces": (), "from_attributes": True}

    id: int
    batch_id: str
    status: str
    # Position of this attempt within its batch (0 = the merchant's initial
    # upload, 1 = their first Regenerate click, etc). Also the index into
    # prompt_builder.RESTYLE_VARIATION_STYLES used for this attempt.
    # Frontend uses this + MAX_IMAGES_PER_BATCH to show "Attempt 2 of 4" and
    # to know when to grey out the Regenerate button.
    variation_index: int
    style_index: int | None = None
    model_used: str | None
    source_image_path: str | None
    is_selected: bool
    image_path: str | None
    cost_usd: float | None
    error_message: str | None


class StyleOut(BaseModel):
    index: int
    label: str


class GenerateRestyleRequest(BaseModel):
    batch_id: str
    style_index: int


class RegenerateRestyleRequest(BaseModel):
    batch_id: str
    # Style the merchant picked for this regenerate (must be one that is still
    # available — see GET /jobs/batch/{batch_id}/styles). Omit / null to keep
    # the old behaviour: the server auto-picks the next unused style, or lets
    # the model choose freely once every curated style has been used.
    style_index: int | None = None


class BatchStylesOut(BaseModel):
    batch_id: str
    # Curated styles NOT yet used in this batch — the options to show.
    styles: list[StyleOut]
    attempts_used: int
    max_attempts: int
    can_regenerate: bool
    # None when can_regenerate is True; otherwise one of:
    # "style_not_chosen" | "generation_in_progress" | "limit_reached"
    blocked_reason: str | None = None
    # True when every curated style is used but regenerate is still allowed:
    # call regenerate WITHOUT style_index and the model picks the surface.
    free_choice_available: bool = False


class SelectRestyleRequest(BaseModel):
    job_id: int