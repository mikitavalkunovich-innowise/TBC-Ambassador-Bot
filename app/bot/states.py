from aiogram.fsm.state import State, StatesGroup


class UserFlow(StatesGroup):
    """FSM states for the user registration flow."""

    selecting_language = State()
    awaiting_privacy = State()
    checking_subscription = State()            # waiting for "I subscribed" confirmation
    awaiting_photo = State()                   # waiting for main selfie (first generation)
    awaiting_extra_photos = State()            # collecting additional angle photos (first gen, optional)
    awaiting_regen_photo = State()             # regen step 1: new main selfie or skip
    awaiting_regen_extra_photos = State()      # regen step 1b: additional angle photos or skip/done
    awaiting_regen_text = State()              # regen step 2: text description or skip
    awaiting_regeneration_input = State()      # holding state between result and regen start
