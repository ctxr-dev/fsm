from ctxr.fsm import FsmSpec, State, Transition

spec = FsmSpec(
    id="t",
    version=1,
    entry="a",
    states=[
        State(id="a", transitions=[Transition(to="b", when="always")]),
        State(id="b"),
    ],
)
