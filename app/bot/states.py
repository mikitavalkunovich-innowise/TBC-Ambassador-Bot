from aiogram.fsm.state import State, StatesGroup


class UserFlow(StatesGroup):
    """FSM states for the user registration flow."""

    selecting_language = State()
    awaiting_privacy = State()
    awaiting_video_action = State()     # after video is sent, waiting for "Generate" button
    checking_subscription = State()     # waiting for "I subscribed" confirmation
    awaiting_photo = State()            # waiting for selfie
    awaiting_regeneration_input = State()  # waiting for new photo or text after a result
