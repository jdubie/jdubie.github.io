"""A render-only YAM with the real YAM Pro meshes (viz/yam_pro, from i2rt's URDF) on the task's kinematic chain.

The two chains share joint senses for joints 1-5; joint 6 turns the other way. Each YAM Pro geom is expressed in the
frame of the matching task body (rotation is exact; position is averaged over poses, the chains' link offsets differ
by up to ~1 cm, which shows as small seams at the joints). Physics is untouched: this model is only for drawing.
"""
from pathlib import Path

import mujoco
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
PRO = ROOT / "viz" / "yam_pro"
PAIRS = {"link1": "link_1", "link2": "link_2", "link3": "link_3", "link4": "link_4", "link5": "link_5",
         "gripper": "link_6", "tip_left": "link_6", "tip_right": "link_6"}
SKIP = {"tip_left_pad", "tip_right_pad"}
ARM_RGBA, GRIP_RGBA = "0.78 0.79 0.81 1", "0.22 0.23 0.25 1"


def _pro_xml() -> str:
    x = (PRO / "yam_pro_urdf_with_linear_gripper.xml").read_text()
    return x.replace('<compiler angle="radian"/>', f'<compiler angle="radian" meshdir="{PRO / "assets"}"/>')


def _pro() -> mujoco.MjModel:
    return mujoco.MjModel.from_xml_string(_pro_xml())


def _q6(q: np.ndarray) -> np.ndarray:
    out = q.copy()
    out[5] = -out[5]
    return out


def _quat(R: np.ndarray) -> np.ndarray:
    q = np.zeros(4)
    mujoco.mju_mat2Quat(q, R.reshape(9))
    return q


def mesh_model(task: mujoco.MjModel, nominal_xml: Path, offwidth: int, offheight: int) -> mujoco.MjModel:
    """Render model: task's chain (with task's body_pos, so the true link lengths), drawn with YAM Pro meshes."""
    pro, nom = _pro(), mujoco.MjModel.from_xml_path(str(nominal_xml))
    spec = mujoco.MjSpec.from_string(_pro_xml())
    xml_pose = {g.meshname: (np.array(g.pos), np.array(g.quat) / np.linalg.norm(g.quat))
                for g in spec.geoms if g.type == mujoco.mjtGeom.mjGEOM_MESH}
    local = {g: xml_pose[pro.mesh(pro.geom_dataid[g]).name] for g in range(pro.ngeom) if pro.geom_dataid[g] >= 0}
    dp, dn = mujoco.MjData(pro), mujoco.MjData(nom)
    rng = np.random.default_rng(0)
    acc: dict[int, list] = {}
    for _ in range(12):
        q = rng.uniform(nom.jnt_range[:6, 0], nom.jnt_range[:6, 1]) * 0.8
        dn.qpos[:6] = q
        dp.qpos[:] = 0
        dp.qpos[:6] = _q6(q)
        mujoco.mj_kinematics(nom, dn)
        mujoco.mj_kinematics(pro, dp)
        for g in range(pro.ngeom):
            mesh = pro.geom_dataid[g]
            body = pro.body(pro.geom_bodyid[g]).name
            if mesh < 0 or pro.mesh(mesh).name in SKIP:
                continue
            if pro.geom_bodyid[g] == 0:
                Rb, pb = np.eye(3), np.zeros(3)
            else:
                b = nom.body(PAIRS[body]).id
                Rb, pb = dn.xmat[b].reshape(3, 3), dn.xpos[b]
            # the XML-level geom pose (before MuJoCo re-centres the mesh), carried by its pro body's world pose
            pbody = pro.geom_bodyid[g]
            Rpb, ppb = dp.xmat[pbody].reshape(3, 3), dp.xpos[pbody]
            gp, gq = local[g]
            Rl = np.zeros(9)
            mujoco.mju_quat2Mat(Rl, gq)
            Rg, pg = Rpb @ Rl.reshape(3, 3), ppb + Rpb @ gp
            acc.setdefault(g, []).append((Rb.T @ Rg, Rb.T @ (pg - pb)))
    geoms: dict[str, list[str]] = {}
    for g, rel in acc.items():
        R, p = rel[0][0], np.mean([r[1] for r in rel], 0)
        body = "world" if pro.geom_bodyid[g] == 0 else PAIRS[pro.body(pro.geom_bodyid[g]).name]
        name = pro.mesh(pro.geom_dataid[g]).name
        rgba = GRIP_RGBA if body == "link_6" or name == "base" else ARM_RGBA
        geoms.setdefault(body, []).append(
            f'<geom type="mesh" mesh="{name}" pos="{" ".join(f"{v:.6f}" for v in p)}" '
            f'quat="{" ".join(f"{v:.6f}" for v in _quat(R))}" rgba="{rgba}"/>')
    assets = "".join(f'<mesh name="{n}" file="{n}.stl"/>' for n in sorted({pro.mesh(pro.geom_dataid[g]).name for g in acc}))
    x = nominal_xml.read_text()
    x = x.replace('<compiler angle="radian"/>', f'<compiler angle="radian" meshdir="{PRO / "assets"}"/>'
                  f'<visual><global offwidth="{offwidth}" offheight="{offheight}"/></visual><asset>{assets}</asset>')
    x = "\n".join(line for line in x.splitlines()
                  if not (line.strip().startswith("<geom") and 'name="floor"' not in line)
                  and not line.strip().startswith("<site"))
    x = x.replace('<body name="arm">', "".join(geoms.get("world", [])) + '<body name="arm">')
    for body, gs in geoms.items():
        if body != "world":
            j = f'<joint name="joint{body[-1]}"'
            i = x.index(j)
            e = x.index("/>", i) + 2
            x = x[:e] + "".join(gs) + x[e:]
    x = x.replace('rgba="0.85 0.86 0.88 1"', 'rgba="0.52 0.53 0.55 1"')  # floor: mid grey, so the light arm and the dark gripper both read
    vm = mujoco.MjModel.from_xml_string(x)
    for i in range(1, task.nbody):
        vm.body_pos[vm.body(task.body(i).name).id] = task.body_pos[i]
    return vm
