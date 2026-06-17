from aiogram.fsm.state import State, StatesGroup


class UserFlow(StatesGroup):
    """FSM states for the user registration flow."""

    selecting_language = State()
    awaiting_privacy = State()
    checking_subscription = State()       # waiting for "I subscribed" confirmation
    awaiting_photo = State()              # waiting for selfie (first generation)
    awaiting_regen_photo = State()        # regen step 1: new selfie or skip
    awaiting_regen_text = State()         # regen step 2: text description or skip
    awaiting_regeneration_input = State() # holding state between result and regen start
