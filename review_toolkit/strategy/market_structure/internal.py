"""Local structure using confirmed pivots and an independently moving boundary."""

from dataclasses import replace

from .structure import Parameters, StructureDetector


class InternalStructureDetector(StructureDetector):
    scope = "internal"

    def __init__(self, parameters=Parameters()):
        super().__init__(replace(parameters, minimum_swing_atr=0.0))

    def advance_boundary(self, swing):
        if (self.boundary is not None and swing.kind == self.boundary.kind
                and swing.pivot_timestamp > self.boundary.pivot_timestamp
                and swing.price != self.boundary.price):
            self.boundary = swing
            self.structural_swings.setdefault(swing, swing.confirmation_timestamp)
