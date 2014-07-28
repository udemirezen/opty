#!/usr/bin/env python

"""This script demonstrates an attempt at identifying the controller for a
two link inverted pendulum on a cart by direct collocation. I collect
"measured" data from the system by simulating it with a known optimal
controller under the influence of random lateral force perturbations. I then
form the optimization problem such that we minimize the error in the model's
simulated outputs with respect to the measured outputs. The optimizer
searches for the best set of controller gains (which are unknown) that
reproduce the motion and ensure the dynamics are valid.

Dependencies this runs with:

    numpy 1.8.1
    scipy 0.14.1
    sympy 0.7.5
    matplotlib 1.3.1
    pydy 0.2.1
    cyipopt 0.1.4

N : number of discretization points
M : number of measured time samples
n : number of states
p : total number of model constants
q : number of free model constants
r : number of free specified inputs
o : number of model outputs

"""

# standard lib
from collections import OrderedDict

# external
import numpy as np
import sympy as sym
from scipy.interpolate import interp1d
from scipy.linalg import solve_continuous_are
from scipy.integrate import odeint
from scipy import sparse
import matplotlib.pyplot as plt
from matplotlib import animation
from matplotlib.patches import Rectangle
from pydy.codegen.code import generate_ode_function
from model import n_link_pendulum_on_cart
import ipopt


def constants_dict(constants):
    """Returns an ordered dictionary which maps the system constant symbols
    to numerical values. The cart sping is set to 10.0 N/m, the cart damper
    to 5.0 Ns/m and gravity is set to 9.81 m/s and the masses and lengths of
    the pendulums are all set to 1.0 kg and meter, respectively."""
    return OrderedDict(zip(constants, [10.0, 5.0, 9.81] + (len(constants) - 1) * [1.0]))


def state_derivatives(states):
    """Returns functions of time which represent the time derivatives of the
    states."""
    return [state.diff() for state in states]


def f_minus_ma(mass_matrix, forcing_vector, states):
    """Returns Fr + Fr* from the mass_matrix and forcing vector."""

    xdot = sym.Matrix(state_derivatives(states))

    return mass_matrix * xdot - forcing_vector


def controllable(a, b):
    """Returns true if the system is controllable and false if not.

    Parameters
    ----------
    a : array_like, shape(n,n)
        The state matrix.
    b : array_like, shape(n,r)
        The input matrix.

    Returns
    -------
    controllable : boolean

    """
    a = np.asmatrix(a)
    b = np.asmatrix(b)
    n = a.shape[0]
    controllability_matrix = []
    for i in range(n):
        controllability_matrix.append(a ** i * b)
    controllability_matrix = np.hstack(controllability_matrix)

    return np.linalg.matrix_rank(controllability_matrix) == n


def compute_controller_gains(num_links):
    """Returns a numerical gain matrix that can be multiplied by the error
    in the states of the n link pendulum on a cart to generate the joint
    torques needed to stabilize the pendulum. The controller follows this
    pattern:

        u(t) = K * [x_eq - x(t)]

    Parameters
    ----------
    n

    Returns
    -------
    K : ndarray, shape(2, n)
        The gains needed to compute joint torques.

    """

    res = n_link_pendulum_on_cart(num_links, cart_force=False,
                                  joint_torques=True, spring_damper=True)

    mass_matrix = res[0]
    forcing_vector = res[1]
    constants = res[2]
    coordinates = res[3]
    speeds = res[4]
    specified = res[5]

    states = coordinates + speeds

    equilibrium_point = np.zeros(len(coordinates) + len(speeds))
    equilibrium_dict = dict(zip(states, equilibrium_point))

    F_A = forcing_vector.jacobian(states)
    F_A = F_A.subs(equilibrium_dict).subs(constants_dict(constants))
    F_A = np.array(F_A.tolist(), dtype=float)

    F_B = forcing_vector.jacobian(specified)
    F_B = F_B.subs(equilibrium_dict).subs(constants_dict(constants))
    F_B = np.array(F_B.tolist(), dtype=float)

    M = mass_matrix.subs(equilibrium_dict).subs(constants_dict(constants))
    M = np.array(M.tolist(), dtype=float)

    invM = np.linalg.inv(M)
    A = np.dot(invM, F_A)
    B = np.dot(invM, F_B)

    assert controllable(A, B)

    Q = np.eye(len(states))
    R = np.eye(len(specified))

    S = solve_continuous_are(A, B, Q, R)

    K = np.dot(np.dot(np.linalg.inv(R), B.T),  S)

    return K


def create_symbolic_controller(states, inputs):
    """"Returns a dictionary with keys that are the joint torque inputs and
    the values are the controller expressions. This can be used to convert
    the symbolic equations of motion from 0 = f(x', x, u, t) to a closed
    loop form 0 = f(x', x, t).

    Parameters
    ----------
    states : sequence of len 2 * (n + 1)
        The SymPy time dependent functions for the system states where n are
        the number of links.
    inputs : sequence of len n
        The SymPy time depednent functions for the system joint torque
        inputs (should not include the lateral force).

    Returns
    -------
    controller_dict : dictionary
        Maps joint torques to control expressions.
    gain_symbols : list of SymPy Symbols
        The symbols used in the gain matrix.
    xeq : list of SymPy Symbols
        The symbols for the equilibrium point.

    """
    num_states = len(states)
    num_inputs = len(inputs)

    xeq = sym.Matrix([x.__class__.__name__ + '_eq' for x in states])

    K = sym.Matrix(num_inputs, num_states, lambda i, j:
                   sym.Symbol('k_{}{}'.format(i, j)))

    x = sym.Matrix(states)
    T = sym.Matrix(inputs)

    gain_symbols = [k for k in K]

    # T = K * (xeq - x) -> 0 = T - K * (xeq - x)

    controller_dict = sym.solve(T - K * (xeq - x), inputs)

    return controller_dict, gain_symbols, xeq


def symbolic_closed_loop(mass_matrix, forcing_vector, states,
                         controller_dict, equilibrium_dict=None):
    """Returns the equation of motion expressions in closed loop form.

    Parameters
    ----------
    equilbrium_dict


    """

    xdot = sym.Matrix(state_derivatives(states))

    if equilibrium_dict is not None:
        for k, v in controller_dict.items():
            controller_dict[k] = v.subs(equilibrium_dict)

    # M * x' = F -> M * x' - F = 0
    system = mass_matrix * xdot - forcing_vector.subs(controller_dict)

    return system


def output_equations(x):
    """Returns the outputs of the system. For now just the an array of the
    generalized coordinates.

    Parameters
    ----------
    x : ndarray, shape(N, n)
        The trajectories of the system states.

    Returns
    -------
    y : ndarray, shape(N, o)
        The trajectories of the generalized coordinates.

    Notes
    -----
    The order of the states is assumed to be:

    [coord_1, ..., coord_{n/2}, speed_1, ..., speed_{n/2}]

    [q_1, ..., q_{n/2}, u_1, ...., u_{n/2}]

    As this is what generate_ode_function creates.

    """

    return x[:, :x.shape[1] / 2]


def closed_loop_ode_func(system, time, set_point, gain_matrix, lateral_force):
    """Returns a function that evaluates the continous closed loop system
    first order ODEs.

    Parameters
    ----------
    system : tuple, len(6)
        The output of the symbolic EoM generator.
    time : ndarray, shape(M,)
        The monotonically increasing time values that
    set_point : ndarray, shape(n,)
        The set point for the controller.
    gain_matrix : ndarray, shape((n - 1) / 2, n)
        The gain matrix that computes the optimal joint torques given the
        system state.
    lateral_force : ndarray, shape(M,)
        The applied lateral force at each time point. This will be linearly
        interpolated for time points other than those in time.

    Returns
    -------
    rhs : function
        A function that evaluates the right hand side of the first order
        ODEs in a form easily used with odeint.
    args : dictionary
        A dictionary containing the model constant values and the controller
        function.

    """

    # TODO : It will likely be useful to allow more inputs: noise on the
    # equilibrium point (i.e. sensor noise) and noise on the joint torques.
    # 10 cycles /sec * 2 pi rad / cycle

    interp_func = interp1d(time, lateral_force)

    def controller(x, t):
        joint_torques = np.dot(gain_matrix, set_point - x)
        if t > time[-1]:
            lateral_force = interp_func(time[-1])
        else:
            lateral_force = interp_func(t)
        return np.hstack((joint_torques, lateral_force))

    rhs = generate_ode_function(*system, generator='cython')

    args = {'constants': np.array(constants_dict(system[2]).values()),
            'specified': controller}

    return rhs, args


def sum_of_sines(magnitudes, frequencies, time):
    sines = np.zeros_like(time)
    for m, w in zip(magnitudes, frequencies):
        sines += m * np.sin(w * time)
    return sines


def animate_pendulum(t, states, length, filename=None):
    """Animates the n-pendulum and optionally saves it to file.

    Parameters
    ----------
    t : ndarray, shape(m)
        Time array.
    states: ndarray, shape(m,p)
        State time history.
    length: float
        The length of the pendulum links.
    filename: string or None, optional
        If true a movie file will be saved of the animation. This may take
        some time.

    """
    # the number of pendulum bobs
    numpoints = states.shape[1] / 2

    # first set up the figure, the axis, and the plot elements we want to
    # animate
    fig = plt.figure()

    # some dimesions
    cart_width = 0.4
    cart_height = 0.2

    # set the limits based on the motion
    xmin = np.around(states[:, 0].min() - cart_width / 2.0, 1)
    xmax = np.around(states[:, 0].max() + cart_width / 2.0, 1)

    # create the axes
    ymin = -length * (numpoints - 1) - 0.1
    ymax = length * (numpoints - 1) + 0.1
    ax = plt.axes(xlim=(xmin, xmax), ylim=(ymin, ymax), aspect='equal')

    # display the current time
    time_text = ax.text(0.04, 0.9, '', transform=ax.transAxes)

    # create a rectangular cart
    rect = Rectangle([states[0, 0] - cart_width / 2.0, -cart_height / 2],
                     cart_width, cart_height,
                     fill=True, color='red', ec='black')
    ax.add_patch(rect)

    # blank line for the pendulum
    line, = ax.plot([], [], lw=2, marker='o', markersize=6)

    # initialization function: plot the background of each frame
    def init():
        time_text.set_text('')
        rect.set_xy((states[0, 0] - cart_width / 2.0,
                     -cart_height / 2.0))
        line.set_data([], [])
        return time_text, rect, line,

    # animation function: update the objects
    def animate(i):
        time_text.set_text('time = {:2.2f}'.format(t[i]))
        rect.set_xy((states[i, 0] - cart_width / 2.0, -cart_height / 2))
        x = np.hstack((states[i, 0], np.zeros((numpoints - 1))))
        y = np.zeros((numpoints))
        for j in np.arange(1, numpoints):
            x[j] = x[j - 1] - length * np.sin(states[i, j])
            y[j] = y[j - 1] + length * np.cos(states[i, j])
        line.set_data(x, y)
        return time_text, rect, line,

    # call the animator function
    anim = animation.FuncAnimation(fig, animate, frames=len(t),
                                   init_func=init,
                                   interval=t[-1] / len(t) * 1000,
                                   blit=False, repeat=False)
    plt.show()

    # save the animation if a filename is given
    if filename is not None:
        anim.save(filename, fps=30, codec='libx264')


def discrete_symbols(states, specified, interval='h'):
    """Returns discrete symbols for each state and specified input along
    with an interval symbol.

    Parameters
    ----------
    states : list of sympy.Functions
        The n functions of time representing the system's states.
    specified : list of sympy.Functions
        The m functions of time representing the system's specified inputs.
    interval : string, optional
        The string to use for the discrete time interval symbol.

    Returns
    -------
    current_states : list of sympy.Symbols
        The n symbols representing the system's ith states.
    previous_states : list of sympy.Symbols
        The n symbols representing the system's (ith - 1) states.
    current_specified : list of sympy.Symbols
        The m symbols representing the system's ith specified inputs.
    interval : sympy.Symbol
        The symbol for the time interval.

    """

    xi = [sym.Symbol(f.__class__.__name__ + 'i') for f in states]
    xp = [sym.Symbol(f.__class__.__name__ + 'p') for f in states]
    si = [sym.Symbol(f.__class__.__name__ + 'i') for f in specified]
    h = sym.Symbol(interval)

    return xi, xp, si, h


def discretize(eoms, states, specified, interval='h'):
    """Returns the constraint equations in a discretized form. Backward
    Euler discretization is used.

    Parameters
    ----------
    states : list of sympy.Functions
        The n functions of time representing the system's states.
    specified : list of sympy.Functions
        The m functions of time representing the system's specified inputs.
    interval : string, optional
        The string to use for the discrete time interval symbol.

    Returns
    -------
    discrete_eoms : sympy.Matrix
        The column vector of the constraint expressions.

    """
    xi, xp, si, h = discrete_symbols(states, specified, interval=interval)

    euler_formula = [(i - p) / h for i, p in zip(xi, xp)]

    # Note : The Derivatives must be substituted before the symbols.
    eoms = eoms.subs(dict(zip(state_derivatives(states), euler_formula)))

    eoms = eoms.subs(dict(zip(states + specified, xi + si)))

    return eoms


def objective_function(free, num_dis_points, num_states, dis_period,
                       time_measured, y_measured):
    """Returns the norm of the difference in the measured and simulated
    output.

    Parameters
    ----------
    free : ndarray, shape(n * N + q,)
        The flattened state array with n states at N time points and the q
        free model constants.
    num_dis_points : integer
        The number of model discretization points.
    num_states : integer
        The number of system states.
    dis_period : float
        The discretization time interval.
    y_measured : ndarray, shape(M, o)
        The measured trajectories of the o output variables at each sampled
        time instance.

    Returns
    -------
    cost : float
        The cost value.

    Notes
    -----
    This assumes that the states are ordered:

    [coord1, ..., coordn, speed1, ..., speedn]

    y_measured is interpolated at the discretization time points and
    compared to the model output at the discretization time points.

    """
    M, o = y_measured.shape
    N, n = num_dis_points, num_states

    sample_rate = 1.0 / dis_period
    duration = (N - 1) / sample_rate

    model_time = np.linspace(0.0, duration, num=N)

    states, specified, constants = parse_free(free, n, 0, N)

    model_state_trajectory = states.T  # states is shape(n, N) so transpose
    model_outputs = output_equations(model_state_trajectory)

    func = interp1d(time_measured, y_measured, axis=0)

    return dis_period * np.sum((func(model_time).flatten() - model_outputs.flatten())**2)


def objective_function_gradient(free, num_dis_points, num_states,
                                dis_period, time_measured, y_measured):
    """Returns the gradient of the objective function with respect to the
    free parameters.

    Parameters
    ----------
    free : ndarray, shape(N * n + q,)
        The flattened state array with n states at N time points and the q
        free model constants.
    num_dis_points : integer
        The number of model discretization points.
    num_states : integer
        The number of system states.
    dis_period : float
        The discretization time interval.
    y_measured : ndarray, shape(M, o)
        The measured trajectories of the o output variables at each sampled
        time instance.

    Returns
    -------
    gradient : ndarray, shape(N * n + q,)
        The gradient of the cost function with respect to the free
        parameters.

    Warning
    -------
    This is currently only valid if the model outputs (and measurements) are
    simply the states. The chain rule will be needed if the function
    output_equations() is more than a simple selection.

    """

    M, o = y_measured.shape
    N, n = num_dis_points, num_states

    sample_rate = 1.0 / dis_period
    duration = (N - 1) / sample_rate

    model_time = np.linspace(0.0, duration, num=N)

    states, specified, constants = parse_free(free, n, 0, N)

    model_state_trajectory = states.T  # states is shape(n, N)

    # coordinates
    model_outputs = output_equations(model_state_trajectory) # shape(N, o)

    func = interp1d(time_measured, y_measured, axis=0)

    dobj_dfree = np.zeros_like(free)
    # Set the derivatives with respect to the coordinates, all else are
    # zero.
    # 2 * (xi - xim)
    dobj_dfree[:N * o] = 2.0 * dis_period * (model_outputs - func(model_time)).T.flatten()

    return dobj_dfree


def wrap_objective(obj_func, *args):
    def wrapped_func(free):
        return obj_func(free, *args)
    return wrapped_func


def general_constraint(eom_vector, state_syms, specified_syms,
                       constant_syms):
    """Returns a function that evaluates the constraints.

    Parameters
    ----------
    discrete_eom_vec : sympy.Matrix, shape(n, 1)
        A column vector containing the discrete symbolic expressions of the
        n constraints.
    state_syms : list of sympy.Functions
        The n functions of time representing the system's states.
    specified_syms : list of sympy.Functions
        The m functions of time representing the system's specified inputs.
    constant_syms : list of sympy.Symbols
        The b symbols representing the system's specified inputs.

    Returns
    -------
    constraints : function
        A function which returns the numerical values of the constraints at
        time points 2,...,N.

    Notes
    -----
    args:
        all current states (x1i, ..., xni)
        all previous states (x1p, ... xnp)
        all current specifieds (s1i, ..., smi)
        constants (c1, ..., cb)
        time interval (h)

        args: (x1i, ..., xni, x1p, ... xnp, s1i, ..., smi, c1, ..., cb, h)
        n: num states
        m: num specified
        b: num constants

    The function should evaluate and return an array:

        [con_1_2, ..., con_1_N, con_2_2, ..., con_2_N, ..., con_n_2, ..., con_n_N]

    for n states and N-1 constraints at the time points.

    """
    xi_syms, xp_syms, si_syms, h = discrete_symbols(state_syms, specified_syms)

    args = [x for x in xi_syms] + [x for x in xp_syms]
    args += [s for s in si_syms] + constant_syms + [h]

    modules = ({'ImmutableMatrix': np.array}, 'numpy')
    f = sym.lambdify(args, eom_vector, modules=modules)

    def constraints(state_values, specified_values, constant_values,
                    interval_value):
        """Returns a vector of constraint values give all of the
        unknowns in the equations of motion over the 2, ..., N time
        steps.

        Parameters
        ----------
        states : ndarray, shape(n, N)
            The array of n states through N time steps.
        specified_values : ndarray, shape(m, N) or shape(N,)
            The array of m specifieds through N time steps.
        constant_values : ndarray, shape(b,)
            The array of b constants.
        interval_value : float
            The value of the dicretization time interval.

        Returns
        -------
        constraints : ndarray, shape(N-1,)
            The array of constraints from t = 2, ..., N.
            [con_1_2, ..., con_1_N, con_2_2, ..., con_2_N, ..., con_n_2, ..., con_n_N]

        """

        if state_values.shape[0] < 2:
            raise ValueError('There should always be at least two states.')

        x_current = state_values[:, 1:]
        x_previous = state_values[:, :-1]

        args = [x for x in x_current] + [x for x in x_previous]

        if len(specified_values.shape) == 2:
            si = specified_values[:, 1:]
            args += [s for s in si]
        else:
            si = specified_values[1:]
            args += [si]

        args += list(constant_values)
        args += [interval_value]

        lam_eval = np.squeeze(f(*args))

        return lam_eval.reshape(x_current.shape[0] * x_current.shape[1])

    return constraints


def general_constraint_jacobian(eom_vector, state_syms, specified_syms,
                                constant_syms, free_constants):
    """Returns a function that evaluates the Jacobian of the constraints.

    Parameters
    ----------
    discrete_eom_vec : sympy.Matrix, shape(n, 1)
        A column vector containing the discrete symbolic expressions of the
        n constraints based on the first order discrete equations of motion.
    state_syms : list of sympy.Functions
        The n functions of time representing the system's states.
    specified_syms : list of sympy.Functions
        The m functions of time representing the system's specified inputs.
    constant_syms : list of sympy.Symbols
        The p symbols representing all of the system's constants.
    free_constants : list of sympy.Symbols
        The q symbols which are a subset of constant_syms that will be free
        to vary in the optimization.

    Returns
    -------
    constraints : function
        A function which returns the numerical values of the constraints at
        time points 2,...,N.

    """
    xi_syms, xp_syms, si_syms, h = discrete_symbols(state_syms, specified_syms)

    # The free parameters are always the n * (N - 1) state values and the
    # user's specified unknown model constants, so the base Jacobian needs
    # to be taken with respect to the ith, and ith - 1 states, and the free
    # model constants.
    # TODO : This needs to eventually support unknown specified inputs too.
    partials = xi_syms + xp_syms + free_constants

    # The arguments to the Jacobian function include all of the free
    # Symbols/Functions in the matrix expression.
    args = xi_syms + xp_syms + si_syms + constant_syms + [h]

    # This ensures that the NumPy array objects are used instead of the
    # NumPy matrix objects.
    modules = ({'ImmutableMatrix': np.array}, 'numpy')

    jac = sym.lambdify(args, eom_vector.jacobian(partials), modules=modules)

    # jac is now a function that takes arguments that are made up of all the
    # variables in the discretized equations of motion. It will be used to
    # build the sparse constraint gradient matrix. This Jacobian function
    # returns the non-zero elements needed to build the sparse constraint
    # gradient.

    num_free_constants = len(free_constants)

    def constraints_jacobian(state_values, specified_values,
                             constant_values, interval_value):
        """Returns a sparse matrix of constraint gradient given all of the
        unknowns in the equations of motion over the 2, ..., N time steps.

        Parameters
        ----------
        states : ndarray, shape(n, N)
            The array of n states through N time steps.
        specified_values : ndarray, shape(m, N) or shape(N,)
            The array of m specified inputs through N time steps.
        constant_values : ndarray, shape(p,)
            The array of p constants.
        interval_value : float
            The value of the dicretization time interval.

        Returns
        -------
        constraints_gradient : scipy.sparse.csr_matrix, shape(2 * (N-1), n * N + p)
            A compressed sparse row matrix containing the gradient of the
            constraints where the constaints are along the rows and the free
            parameters are along the columns.

        """

        if state_values.shape[0] < 2:
            raise ValueError('There should always be at least two states.')

        x_current = state_values[:, 1:]  # n x N - 1
        x_previous = state_values[:, :-1]  # n x N - 1

        num_states = state_values.shape[0]  # n
        num_time_steps = state_values.shape[1]  # N

        num_constraints = num_states * (num_time_steps - 1)
        num_free = num_states * num_time_steps + num_free_constants

        jacobian_matrix = sparse.lil_matrix((num_constraints, num_free))

        # Now loop through the N - 1 constraints to compute the non-zero
        # entries to the gradient matrix (the partials for n states will be
        # computed at each iteration).

        for i in range(num_time_steps - 1):
            # n: num_states
            # m: num_specified
            # p: num_free_constants

            xi = x_current[:, i]  # len(n)
            xp = x_previous[:, i]  # len(n)
            if len(specified_values.shape) < 2:
                si = specified_values[i]  # len(m)
            else:
                si = specified_values[:, i]  # len(m)

            args = np.hstack((xi, xp, si, constant_values, interval_value))

            non_zero_derivatives = jac(*args)  # n x (2*n+p), p is len(free_constants)

            # the states repeat every N - 1 constraints
            # row_idxs = [0 * (N - 1), 1 * (N - 1),  2 * (N - 1),  n * (N - 1)]

            row_idxs = [j * (num_time_steps - 1) + i
                        for j in range(num_states)]

            # The derivative columns are in this order:
            # [x1i, x2i, ..., xni, x1p, x2p, ..., xnp, p1, ..., pp]
            # So we need to map them to the correct column.

            # first row, the columns indices mapping is:
            # [1, N + 1, ..., N - 1] : [x1p, x1i, 0, ..., 0]
            # [0, N, ..., 2 * (N - 1)] : [x2p, x2i, 0, ..., 0]
            # [-p:] : p1,..., pp  the free constants

            # i=0: [1, ..., n * N + 1, 0, ..., n * N + 0, n * N:n * N + p]
            # i=1: [2, ..., n * N + 2, 1, ..., n * N + 1, n * N:n * N + p]
            # i=2: [3, ..., n * N + 3, 2, ..., n * N + 2, n * N:n * N + p]

            col_idxs = [j * num_time_steps + i + 1 for j in range(num_states)]
            col_idxs += [j * num_time_steps + i for j in range(num_states)]
            col_idxs += [num_states * num_time_steps + j
                         for j in range(num_free_constants)]

            substitute_matrix(jacobian_matrix, row_idxs, col_idxs,
                              non_zero_derivatives)

        return jacobian_matrix.tocsr()

    return constraints_jacobian


def wrap_constraint(func, num_time_steps, num_states,
                    interval_value, constant_syms, specified_syms,
                    fixed_constants, fixed_specified):
    """Returns a function that evaluates all of the constraints or Jacobian
    of the constraints given the system's free parameters.

    Parameters
    ----------
    func : function
        A function that takes the full parameter set an evaulates the
        constraint functions or the Jacobian of the contraint functions.
        i.e. the output of general_constraint or general_jacobian.
    num_time_steps : integer
        The number of time steps.
    num_states : integer
        The number of states in the system.
    interval_value : float
        The interval between the time steps.
    constant_syms : list of sympy.Symbols
        A list of all the constants in system constraint equations.
    specified_syms : list of sympy.Functions
        A list of all the discrete specified inputs.
    fixed_constants : dictionary
        A map of all the system constants which are not free optimization
        parameters to their fixed values.
    fixed_specified : dictionary
        A map of all the system's discrete specified inputs that are not
        free optimization parameters to their fixed values.

    Returns
    -------
    func : function
        A function which returns constraint values given the system's free
        parameters.

    """

    num_free_specified = len(specified_syms) - len(fixed_specified)

    def constraints(free):
        """

        Parameters
        ----------
        free : ndarray

        Returns
        -------
        constraints : ndarray, shape(N-1,)
            The array of constraints from t = 2, ..., N.
            [con_1_2, ..., con_1_N, con_2_2, ..., con_2_N, ..., con_n_2, ..., con_n_N]
        """

        free_states, free_specified, free_constants = \
            parse_free(free, num_states, num_free_specified, num_time_steps)

        all_specified = merge_fixed_free(specified_syms, fixed_specified,
                                         free_specified)

        all_constants = merge_fixed_free(constant_syms, fixed_constants,
                                         free_constants)

        return func(free_states, all_specified, all_constants, interval_value)

    return constraints


def merge_fixed_free(syms, fixed, free):
    """Returns an array with the fixed and free

    This assumes that you have the free constants in the correct order.

    """

    merged = []
    n = 0
    for i, s in enumerate(syms):
        if s in fixed.keys():
            merged.append(fixed[s])
        else:
            merged.append(free[n])
            n += 1
    return np.array(merged)


def parse_free(free, n, r, N):
    """Parses the free parameters vector and returns it's components.

    free : ndarray, shape(n * N + m * M + q)
        The free parameters of the system.
    n : integer
        The number of states.
    r : integer
        The number of free specified inputs.
    N : integer
        The number of time steps.

    Returns
    -------
    states : ndarray, shape(n, N)
        The array of n states through N time steps.
    specified_values : ndarray, shape(r, N) or shape(N,), or None
        The array of r specified inputs through N time steps.
    constant_values : ndarray, shape(q,)
        The array of q constants.

    """

    len_states = n * N
    len_specified = r * N

    free_states = free[:len_states].reshape((n, N))

    if r == 0:
        free_specified = None
    else:
        free_specified = free[len_states:len_states + len_specified]
        if r > 1:
            free_specified = free_specified.reshape((r, N))

    free_constants = free[len_states + len_specified:]

    return free_states, free_specified, free_constants


def substitute_matrix(matrix, row_idxs, col_idxs, sub_matrix):
    """Returns the matrix with the values given by the row and column
    indices with those in the sub-matrix.

    Parameters
    ----------
    matrix : ndarray, shape(n, m)
        A matrix (i.e. 2D array).
    row_idxs : array_like, shape(p<=n,)
        The row indices which designate which entries should be replaced by
        the sub matrix entries.
    col_idxs : array_like, shape(q<=m,)
        The column indices which designate which entries should be replaced
        by the sub matrix entries.
    sub_matrix : ndarray, shape(p, q)
        A matrix of values to substitute into the specified rows and
        columns.

    Notes
    -----
    This makes a copy of the sub_matrix, so if it is large it may be slower
    than a more optimal implementation.

    Examples
    --------

    >>> a = np.zeros((3, 4))
    >>> sub = np.arange(4).reshape((2, 2))
    >>> substitute_matrix(a, [1, 2], [0, 2], sub)
    array([[ 0.,  0.,  0.,  0.],
           [ 0.,  0.,  1.,  0.],
           [ 2.,  0.,  3.,  0.]])

    """

    assert sub_matrix.shape == (len(row_idxs), len(col_idxs))

    row_idx_permutations = np.repeat(row_idxs, len(col_idxs))
    col_idx_permutations = np.array(list(col_idxs) * len(row_idxs))

    matrix[row_idx_permutations, col_idx_permutations] = sub_matrix.flatten()

    return matrix


def plot_sim_results(y, u):

    # Plot the simulation results and animate the pendulum.
    fig, axes = plt.subplots(3, 1)
    axes[0].plot(u)
    axes[0].set_ylabel('Lateral Force [N]')
    axes[1].plot(y[:, 0])
    axes[1].set_ylabel('Cart Displacement [M]')
    axes[2].plot(np.rad2deg(y[:, 1:]))
    axes[2].set_ylabel('Link Angles [Deg]')
    axes[2].set_xlabel('Time [s]')
    plt.tight_layout()

    plt.show()


class Problem(ipopt.problem):

    def __init__(self, N, n, q, obj, obj_grad, con, con_jac):
        """

        Parameters
        ----------
        num_discretization_points
        num_states
        num_free_model_parameters
        obj : function
            The objective function.
        obj_grad : function

        """

        num_free_variables = n * N + q
        num_constraints = n * (N-1)

        self.obj = obj
        self.obj_grad = obj_grad
        self.con = con
        self.con_jac = con_jac

        self.con_jac_rows, self.con_jac_cols, values = \
            sparse.find(con_jac(np.random.random(num_free_variables)))

        con_bounds = np.zeros(num_constraints)

        super(Problem, self).__init__(n=num_free_variables,
                                      m=num_constraints,
                                      cl=con_bounds,
                                      cu=con_bounds)

        #self.addOption('derivative_test', 'first-order')
        self.addOption('linear_solver', 'ma57')

        self.obj_value = []

    def objective(self, free):
        return self.obj(free)

    def gradient(self, free):
        # This should return a column vector.
        return self.obj_grad(free)

    def constraints(self, free):
        # This should return a column vector.
        return self.con(free)

    def jacobianstructure(self):
        return (self.con_jac_rows, self.con_jac_cols)

    def jacobian(self, free):
        jac = self.con_jac(free)
        return sparse.find(jac)[2]

    def intermediate(self, *args):
        self.obj_value.append(args[2])


def plot_constraints(constraints, n, N, state_syms):
    """Plots the constrain violations for each state."""
    cons = constraints.reshape(n, N - 1).T
    plt.plot(range(2, cons.shape[0] + 2), cons)
    plt.ylabel('Constraint Violation')
    plt.xlabel('Discretization Point')
    plt.legend([str(s) for s in state_syms])
    plt.show()


def input_force(typ, time):

    if typ == 'sine':
        lateral_force = 8.0 * np.sin(3.0 * 2.0 * np.pi * time)
    elif typ == 'random':
        lateral_force = 8.0 * np.random.random(len(time))
        lateral_force -= lateral_force.mean()
    elif typ == 'zero':
        lateral_force = np.zeros_like(time)
    elif typ == 'sumsines':
        # I took these frequencies from a sum of sines Ron designed for a
        # pilot control problem.
        nums = [7, 11, 16, 25, 38, 61, 103, 131, 151, 181, 313, 523]
        freq = 2 * np.pi * np.array(nums) / 240
        mags = 2.0 * np.ones(len(freq))
        lateral_force = sum_of_sines(mags, freq, time)
    else:
        raise ValueError('{} is not a valid force type.'.format(typ))

    return lateral_force


if __name__ == "__main__":

    import argparse

    parser = argparse.ArgumentParser(description="Run ")

    parser.add_argument('-', '--mocapfile', type=str,
        help="The path to a D-Flow mocap module output file.", default=None)

    parser.add_argument('-d', '--duration', type=float,
        help="The duration of the simulation in seconds.", default=1.0)

    parser.add_argument('-s', '--samplerate', type=float,
        help="The sample rate of the discretization.", default=500.0)

    parser.add_argument('-a', '--animate', type=bool,
        help="The sample rate of the discretization.", default=500.0)

    #parser.add_argument('outputfile', type=str,
                        #help="The path to the output file.")

    args = parser.parse_args()

    num_links = 2

    # Specify the number of time steps and duration of the measurements.
    sample_rate = 50  # hz
    duration = 30.0  # seconds
    num_time_steps = int(duration * sample_rate) + 1
    discretization_interval = 1.0 / sample_rate
    time = np.linspace(0.0, duration, num=num_time_steps)

    # Generate the symbolic equations of motion for the two link pendulum on
    # a cart.
    system = n_link_pendulum_on_cart(num_links, cart_force=True,
                                     joint_torques=True, spring_damper=True)

    mass_matrix = system[0]
    forcing_vector = system[1]
    constants_syms = system[2]
    coordinates_syms = system[3]
    speeds_syms = system[4]
    specified_inputs_syms = system[5]  # last entry is lateral force

    states_syms = coordinates_syms + speeds_syms

    num_states = len(states_syms)

    # Find some optimal gains for stablizing the pendulum on the cart.
    print('Finding the optimal gains.')
    gains = compute_controller_gains(num_links)

    # Generate some "measured" data from the simulation.
    print('Simulating the system.')

    lateral_force = input_force('sumsines', time)

    set_point = np.zeros(num_states)

    initial_conditions = np.zeros(num_states)
    offset = 10.0 * np.random.random((num_states / 2) - 1)
    initial_conditions[1:num_states / 2] = np.deg2rad(offset)

    rhs, args = closed_loop_ode_func(system, time, set_point, gains, lateral_force)

    x = odeint(rhs, initial_conditions, time, args=(args,))
    y = output_equations(x)
    u = lateral_force

    print('Forming the constraint function.')
    # Generate the expressions for creating the closed loop equations of
    # motion.
    control_dict, gain_syms, equil_syms = \
        create_symbolic_controller(states_syms, specified_inputs_syms[:-1])

    num_gains = len(gain_syms)

    eq_dict = dict(zip(equil_syms, num_states * [0]))

    # This is the symbolic closed loop continuous system.
    closed = symbolic_closed_loop(mass_matrix, forcing_vector, states_syms,
                                  control_dict, eq_dict)

    # This is the discretized (backward euler) version of the closed loop
    # system.
    dclosed = discretize(closed, states_syms, specified_inputs_syms)

    # Now generate a function which evaluates the N-1 constraints.
    gen_con_func = general_constraint(dclosed, states_syms,
                                      [specified_inputs_syms[-1]],
                                      constants_syms + gain_syms)

    con_func = wrap_constraint(gen_con_func,
                               num_time_steps,
                               len(states_syms),
                               discretization_interval,
                               constants_syms + gain_syms,
                               [specified_inputs_syms[-1]],
                               constants_dict(constants_syms),
                               {specified_inputs_syms[-1]: u})

    gen_con_jac_func = general_constraint_jacobian(dclosed,
                                                   states_syms,
                                                   [specified_inputs_syms[-1]],
                                                   constants_syms + gain_syms,
                                                   gain_syms)

    con_jac_func = wrap_constraint(gen_con_jac_func,
                                   num_time_steps,
                                   len(states_syms),
                                   discretization_interval,
                                   constants_syms + gain_syms,
                                   [specified_inputs_syms[-1]],
                                   constants_dict(constants_syms),
                                   {specified_inputs_syms[-1]: u})

    print('Forming the objective function.')

    obj_func = wrap_objective(objective_function,
                              len(time),
                              num_states,
                              discretization_interval,
                              time,
                              y)

    obj_grad_func = wrap_objective(objective_function_gradient,
                                   len(time),
                                   num_states,
                                   discretization_interval,
                                   time,
                                   y)


    print('Solving optimization problem.')

    prob = Problem(num_time_steps, num_states, num_gains, obj_func,
                   obj_grad_func, con_func, con_jac_func)


    initial_guess = np.hstack((x.T.flatten(), gains.flatten()))
    initial_guess = np.hstack((x.T.flatten(), np.ones_like(gains.flatten())))
    initial_guess = np.hstack((x.T.flatten(), np.random.random(len(gains.flatten()))))

    #solution, info = prob.solve(initial_guess)

    print("Known gains: {}".format(gains))

    #sol_states, sol_specified, sol_constants = parse_free(solution, num_states, 0, num_time_steps)
    #sol_gains = sol_constants.reshape(gains.shape)
#
    #print("Identified gains: {}".format(sol_gains))

    #animate_pendulum(np.linspace(0.0, duration, num_time_steps), x, 1.0)
