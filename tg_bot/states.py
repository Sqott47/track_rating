from aiogram.fsm.state import StatesGroup, State

class SubmitTrack(StatesGroup):
    waiting_file = State()
    waiting_artist = State()
    waiting_title = State()
    confirm_meta = State()
    choose_priority = State()
    choose_payment_method = State()
    waiting_payment = State()

class RaisePriority(StatesGroup):
    choosing_track = State()
    choosing_priority = State()
    choosing_payment_method = State()
    waiting_payment = State()
