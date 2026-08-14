"""基准策略：随机行动（用于进化评估的对手）。"""

import random

from .base import Strategy


class RandomBot(Strategy):
    name = "random"

    def __init__(self, seed=0):
        self.rng = random.Random(seed)

    def reset(self):
        pass

    def decide(self, obs):
        plan = {"core": None, "units": {}}
        directions = ["UP", "DOWN", "LEFT", "RIGHT"]
        if obs.core and obs.core["migration"] is None:
            r = self.rng.random()
            if r < 0.15 and obs.core["resources"] >= 5:
                plan["core"] = ("SPAWN", {"unit_type": self.rng.choice(
                    ["WORKER", "VANGUARD", "RANGER"])})
            elif r < 0.30:
                plan["core"] = ("START_MOVE", {"direction": self.rng.choice(directions)})
            elif r < 0.40 and obs.core["hp"] < 5:
                plan["core"] = ("HEAL", {})
        for u in obs.units:
            if u["just_spawned"] if False else False:
                continue
            r = self.rng.random()
            if u["utype"] == "WORKER":
                if u["pos"] in obs.resources and u["cargo"] == 0:
                    plan["units"][u["uid"]] = ("HARVEST", {})
                elif u["cargo"] > 0:
                    plan["units"][u["uid"]] = ("DEPOSIT", {})
                else:
                    plan["units"][u["uid"]] = ("MOVE", {"direction":
                                                        self.rng.choice(directions)})
            elif u["utype"] == "VANGUARD":
                if r < 0.5:
                    plan["units"][u["uid"]] = ("MOVE", {"direction":
                                                        self.rng.choice(directions)})
                else:
                    plan["units"][u["uid"]] = ("SWEEP", {"direction":
                                                         self.rng.choice(directions)})
            else:
                if r < 0.5:
                    plan["units"][u["uid"]] = ("MOVE", {"direction":
                                                        self.rng.choice(directions)})
                else:
                    x, y = u["pos"]
                    dx, dy = self.rng.choice([(1, 0), (-1, 0), (0, 1), (0, -1)])
                    plan["units"][u["uid"]] = ("SHOOT", {
                        "expected_cell": [x + dx * self.rng.randint(1, 3),
                                          y + dy * self.rng.randint(1, 3)]})
        return plan


class WaitBot(Strategy):
    """什么都不做：仅用于 sanity 测试。"""

    name = "wait"

    def decide(self, obs):
        return {"core": None, "units": {}}
