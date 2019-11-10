"""Python implementation of the nonlinear cost Spiess and Florian model.

Based on the nonlinear cost version of the transit assignment model from
"H. Spiess and M. Florian. Optimal Strategies: A New Assignment Model for
Transit Networks. Transportation Research Part B: Methodological,
23B(2):83-102, 1989."
"""

########################################################################################
# Update documentation (this will be in the thesis)

# Note: This import path assumes that this module is being called from within
# the main driver module.
import constraints.assignment.spiess_constant as sc
import numpy as np
import scipy.optimize as op

#==============================================================================
class Spiess:
    """The main public class for the nonlinear cost Spiess module.

    The purpose of this class is to create an object capable of maintaining and
    updating the problem data, applying the nonlinear Spiess and Florian model
    to the given network, and outputting an arc flow vector.

    It also makes use of the constant-cost module as a subroutine of the
    solution method. Because the constant-cost module already reads in the
    problem information and possesses classes for maintaining network
    structures, none of that will be done in this module.
    """

    #--------------------------------------------------------------------------
    def __init__(self, data="", cplex_epsilon=0.001, optimality_epsilon=0.1,
                 max_iterations=100):
        """Nonlinear cost Spiess constructor.

        Initializes a constant-cost Spiess object, which in turn reads in most
        of the problem information.

        Accepts the following optional keyword arguments:
            data -- Root directory containing the network information. Defaults
                to the current working directory.
            cplex_epsilon -- Epsilon value for CPLEX solver's cleanup method.
                Values generated by the solver falling below this absoulte
                value are deleted between solves. Defaults to 0.001.
            optimality_epsilon -- Epsilon value for the Frank-Wolfe solution
                solution algorithm of the nonlinear cost model. The algorithm
                terminates as soon as the absolute optimality gap falls below
                this value (or upon reaching the iteration cutoff). Defaults to
                0.1.
            max_iterations -- Maximum number of iterations of the Frank-Wolfe
                solution algorithm. The algorithm cuts off at this point if it
                has not yet achieved the desired optimality gap. Defaults to
                100.
        """

        ##################################################################################
        # See about "typical" objective values for this problem and base the epsilon value on that order of magnitude.

        self.data = data
        self.epsilon = optimality_epsilon
        self.max_iterations = max_iterations

        # Initialize constant-cost Spiess object (note that this also loads the
        # network information into the submodel)
        self.Submodel = sc.SpiessConstant(data=self.data,
                                          cplex_epsilon=cplex_epsilon)

        # Initialize flow vector and waiting time scalar
        self.flows, self.waiting = self.Submodel.calculate()

        # Read in rest of problem data
        self._load_data()

    #--------------------------------------------------------------------------
    def __del__(self):
        """Nonlinear Spiess object destructor. Deletes the submodel object."""

        del self.Submodel

    #--------------------------------------------------------------------------
    def _load_data(self):
        """Reads model and line information from the data files.

        Reads in the arc cost parameters and the line capacities. Initializes
        the line capacity vector. All other required input data is loaded by
        the constant-cost module and can be accessed through the Submodel
        object.
        """

        # Read in conical congestion parameter
        with open(self.data+"Problem_Data.txt", 'r') as f:
            for i in range(11):
                # Skip first few lines
                f.readline()
            self.conical_alpha = float(f.readline())

        # Calculate second conical congestion parameter
        self.conical_beta = (2*self.conical_alpha-1)/(2*self.conical_alpha-2)

        # Initialize line capacity list
        self.capacity = []
        i = -1
        with open(self.data+"Transitdata.txt", 'r') as f:
            for line in f:
                i += 1
                if i > 0:
                    # Skip comment line
                    self.capacity.append(float(line.split()[2]))

    #--------------------------------------------------------------------------
    def _arc_cost(self, base, flow, capacity):
        """Nonlinear arc cost function.

        This is a conical congestion function for the line arc costs. It should
        cause the cost of a line arc to sharply increase as it approaches its
        capacity, discouraging too many people from using the same line and
        helping to enforce line capacity.

        Requires, in order: the line's base cost, the current arc flow, and the
        line capacity.
        """

        ratio = 1 - flow/capacity

        return base*(2 + np.sqrt((self.conical_alpha*ratio)**2
                                 + self.conical_beta**2) - self.conical_alpha
                                 *ratio - self.conical_beta)

    #--------------------------------------------------------------------------
    def _arc_cost_prime(self, base, flow, capacity):
        """Derivative of the arc cost function (with respect to flow).

        This derivative is used in the process of finding the optimal convex
        combination of the old and new solutions. Specifically, it is used to
        apply a derivative-based root finding method to annulling the
        objective's derivative.
        """

        ratio = 1 - flow/capacity

        return base*(-(ratio*(self.conical_alpha**2))/(capacity*np.sqrt((ratio*
                self.conical_alpha)**2 + self.conical_beta**2)) +
                self.conical_alpha/capacity)

    #--------------------------------------------------------------------------
    def _obj_prime(self, parameter, flow_new, wait_new):
        """Nonlinear cost objective first derivative (w.r.t. convex parameter).

        This is the first derivative of the nonlinear cost model's objective
        (with respect to the convex combination parameter) evaluated at a
        convex combination of the current solution and a new solution . Our
        method for finding the optimal convex combination is to find the root
        of this derivative.

        The inputs, in order, are: the convex combination parameter, the new
        flow vector, and the new waiting time. Only the convex combination will
        be treated as a variable by the root finder.
        """

        t = wait_new - self.waiting
        for i in range(len(self.Submodel.arcs)):
            a = self.Submodel.arcs[i]
            t += (flow_new[i]-self.flows[i])*self._arc_cost(a.cost,
                   (1-parameter)*self.flows[i]+parameter*flow_new[i],
                   self.capacity[a.line])
        return t

    #--------------------------------------------------------------------------
    def _obj_prime2(self, parameter, flow_new, wait_new):
        """Nonlinear cost objective second derivative.

        Analogous to the first derivative function, but now the second
        derivative with respect to the convex combination parameter. Used by
        a derivative-based root finding method to find the root of the first
        derivative.
        """

        t = 0.0
        for i in range(len(self.Submodel.arcs)):
            a = self.Submodel.arcs[i]
            t += ((flow_new[i]-self.flows[i])**2)*self._arc_cost_prime(a.cost,
                 (1-parameter)*self.flows[i]+parameter*flow_new[i],
                 self.capacity[a.line])
        return t

    #--------------------------------------------------------------------------
    def _update_arc_costs(self):
        """Updates all arc costs in the Submodel based on the current flows.

        This process involves going through each arc, using the arc cost
        function to calculate its cost, and then passing the vector of results
        to the Submodel to update its LP.
        """

        cost = [0.0 for a in self.Submodel.arcs] # initialize new cost vector

        for i in range(len(self.Submodel.arcs)):
            # Calculate cost of each arc one-at-a-time
            a = self.Submodel.arcs[i]
            cost[i] = self._arc_cost(a.cost, self.flows[i],
                self.capacity[a.line])

        self.Submodel.update_cost(cost) # pass new cost vector to Submodel

    #--------------------------------------------------------------------------
    def update_lines(self, freq, cap):
        """Updates the model for new line frequency and capacity vectors.

        Each solution produced by the main search algorithm defines a frequency
        and capacity for each line. These are calculated within the constraint
        module and can be passed directly to this solver. The update process
        requires changing the waiting time coefficients in the waiting time
        constraints.

        This method is called by the constraints module, which calculates the
        frequency and capacity for a given solution. It must also pass these
        new attributes to the submodel LP.
        """

        self.Submodel.update_lines(freq) # update submodel
        self.capacity = cap # update own capacities

    #--------------------------------------------------------------------------
    def _optimal_step(self, flow_new, wait_new):
        """Returns the optimal convex combination of old and new solutions.

        Accepts a flow vector and waiting time, and returns the convex
        combination of the current flow/wait values and these new values.
        'Optimal' here means that it minimizes the objective value of the
        nonlinear cost Spiess and Florian model.

        The objective is convex, so we need only search for the root of its
        derivative with respect to the convex combination parameter. If this
        occurs for a parameter value outside of [0,1], then the optimum occurs
        at an endpoint of the interval, in which case we just take the endpoint
        closer to the root.

        In case the root finding process fails to converge, we will default to
        a method of successive averages convex combination (1/n new and 1-1/n
        old for iteration n).
        """

        # Attempt to annull the objective derivative w.r.t. the convex param
        root_sol = op.root_scalar(self._obj_prime, (flow_new, wait_new),
                                  method='newton', x0=1.0,
                                  fprime=self._obj_prime2)

        # Choose convex combination parameter
        if root_sol.converged == True:
            # Take final parameter value, restricted to [0,1]
            parameter = max(0.0, min(1.0, root_sol.root))
        else:
            # Default to MSA parameter
            parameter = 1/self.iteration

        # Return convex combination
        return ((1-parameter)*self.flows + parameter*flow_new,
                (1-parameter)*self.waiting + parameter*wait_new)

    #--------------------------------------------------------------------------
    def _optimality_gap(self, flow_new, wait_new):
        """Returns the optimality gap given the updated flow and waiting time.

        This is an upper bound for the absolute optimality gap, for use in
        deciding when to terminate the Frank-Wolfe algorithm. It should be
        evaluated before setting the updated solutions as current, since it
        relies on evaluating differences between the two.
        """

        return -self._obj_prime(0, flow_new, wait_new)

    #--------------------------------------------------------------------------
    def calculate(self):
        """Solves the nonlinear cost model and outputs the total results.

        The NLP here concerns a transit network G = (V,E) for which we want to
        calculate all arc flows. Unlike with the constant-cost version, we
        cannot separate this program by destination node since all flows must
        be summed to calculate the arc costs. Most of the program has the same
        form as the constant-cost LP, except that all flow variables and
        waiting time variables are now indexed by destination r, and we define
        the total arc flows as v_ij = sum_{r in R} v_ij^r. The objective is:

            min  sum_{ij in E} int_0^{v_ij} c_ij(x) dx
                     + sum_{i in V} sum_{r in R} w_i^r

        c_ij(x) is the nonlinear arc cost function for arc ij, which should be
        a continuous, nondecreasing function of the flow through that arc. For
        any such cost functions, the NLP is convex, and so we may use the
        Frank-Wolfe algorithm. It is an iterative process similar to gradient
        descent, defined as follows:

            Step 0: Begin with an initial feasible solution (consisting of an
                arc flow vector along with a total waiting time scalar).
            Step 1: Calculate the nonlinear arc costs based on the current
                flow vector, then solve the constant-cost version of the
                model (via our Subproblem) using this set of costs.
            Step 2: Find the optimal convex combination of the constant-cost
                solution and the current solution.
            Step 3: Set the chosen convex combination as the current solution.
                If the optimality gap falls below the chosen cutoff value, then
                terminate and output the current solution. Otherwise, return to
                Step 1.

        The output is a tuple consisting of a NumPy array of total arc flows
        (in the order of the arc list) and the total waiting time (as a
        scalar), respectively.
        """

        # Initialize solution vector
        self._update_arc_costs()
        self.flows, self.waiting = self.Submodel.calculate()

        self.iteration = 0
        gap = np.inf

        # Main loop
        while (gap > self.epsilon) and (self.iteration < self.max_iterations):
            # Continue until achieving a small enough optimality gap or
            # reaching the iteration cutoff

            self.iteration += 1

            # Update costs and solve constant-cost model
            self._update_arc_costs()
            cc_flows, cc_waiting = self.Submodel.calculate()

            # Find optimal convex combination
            new_flows, new_waiting = self._optimal_step(cc_flows, cc_waiting)

            # Calculate optimality gap and move to new solution
            gap = self._optimality_gap(cc_flows, cc_waiting)
            self.flows, self.waiting = new_flows, new_waiting

        return self.flows, self.waiting