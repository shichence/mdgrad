"""Summary
"""
import torch
import torchmd
from nff.nn.layers import GaussianSmearing
import numpy as np
from torchmd.system import check_system
from torchmd.topology import generate_nbr_list, get_offsets, generate_angle_list

def generate_vol_bins(start, end, nbins, dim):
    bins = torch.linspace(start, end, nbins + 1)
    
    # compute volume differential 
    if dim == 3:
        Vbins = 4 * np.pi /3*(bins[1:]**3 - bins[:-1]**3)
        V = (4/3)* np.pi * (end) ** 3
    elif dim == 2:
        Vbins = np.pi * (bins[1:]**2 - bins[:-1]**2)
        V = np.pi * (end) ** 2
        
    return V, torch.Tensor(Vbins), bins


class Observable(torch.nn.Module):
    def __init__(self, system):
        super(Observable, self).__init__()
        check_system(system)
        self.device = system.device
        self.volume = system.get_volume()
        self.cell = torch.Tensor( system.get_cell()).diag().to(self.device)
        self.natoms = system.get_number_of_atoms()

class rdf(Observable):
    def __init__(self, system, nbins, r_range, index_tuple=None, width=None):
        super(rdf, self).__init__(system)
        PI = np.pi

        start = r_range[0]
        end = r_range[1]
        self.device = system.device

        V, vol_bins, bins = generate_vol_bins(start, end, nbins, dim=system.dim)

        self.V = V
        self.vol_bins = vol_bins.to(self.device)
        self.r_axis = np.linspace(start, end, nbins)
        self.device = system.device
        self.bins = bins

        self.smear = GaussianSmearing(
            start=start,
            stop=bins[-1],
            n_gaussians=nbins,
            width=width,
            trainable=False
        ).to(self.device)

        self.nbins = nbins
        self.cutoff_boundary = end + 5e-1
        self.index_tuple = index_tuple
        
    def forward(self, xyz):

        nbr_list, pair_dis, _ = generate_nbr_list(xyz, 
                                               self.cutoff_boundary, 
                                               self.cell, 
                                               index_tuple=self.index_tuple, 
                                               get_dis=True)

        count = self.smear(pair_dis.reshape(-1).squeeze()[..., None]).sum(0) 
        norm = count.sum()   # normalization factor for histogram 
        count = count / norm   # normalize 
        count = count
        rdf =  count / (self.vol_bins / self.V )  

        return count, self.bins, rdf 

class Angles(Observable):
    def __init__(self, system, nbins, angle_range, cutoff=3.0,index_tuple=None, width=None):
        super(Angles, self).__init__(system)
        PI = np.pi
        start = angle_range[0]
        end = angle_range[1]
        self.device = system.device
        self.bins = torch.linspace(start, end, nbins + 1).to(self.device)
        self.smear = GaussianSmearing(
            start=start,
            stop=self.bins[-1],
            n_gaussians=nbins,
            width=width,
            trainable=False
        ).to(self.device)
        self.width = (self.smear.width[0]).item()
        self.cutoff = cutoff
        self.index_tuple = index_tuple
        
    def forward(self, xyz):
        
        xyz = xyz.reshape(-1, self.natoms, 3)

        nbr_list, _ = generate_nbr_list(xyz, self.cutoff,
                                           self.cell, 
                                           index_tuple=self.index_tuple, 
                                           get_dis=False)
        nbr_list = nbr_list.to("cpu")
        
        angle_list = generate_angle_list(nbr_list).to(self.device)
        cos_angles = compute_angle(xyz, angle_list, self.cell, N=self.natoms)

        return cos_angles

class angle_distribution(Observable):
    def __init__(self, system, nbins, angle_range, cutoff=3.0,index_tuple=None, width=None):
        super(angle_distribution, self).__init__(system)
        PI = np.pi
        start = angle_range[0]
        end = angle_range[1]
        self.device = system.device
        self.bins = torch.linspace(start, end, nbins + 1).to(self.device)
        self.smear = GaussianSmearing(
            start=start,
            stop=self.bins[-1],
            n_gaussians=nbins,
            width=width,
            trainable=False
        ).to(self.device)
        self.width = (self.smear.width[0]).item()
        self.cutoff = cutoff
        self.index_tuple = index_tuple
        
    def forward(self, xyz):
        
        xyz = xyz.reshape(-1, self.natoms, 3)

        nbr_list, _ = generate_nbr_list(xyz, self.cutoff,
                                           self.cell, 
                                           index_tuple=self.index_tuple, 
                                           get_dis=False)
        nbr_list = nbr_list.to("cpu")
        
        angle_list = generate_angle_list(nbr_list).to(self.device)
        cos_angles = compute_angle(xyz, angle_list, self.cell, N=self.natoms)
        
        angles = cos_angles.acos()

        count = self.smear(angles.reshape(-1).squeeze()[..., None]).sum(0) 

        norm = count.sum()   # normalization factor for histogram 
        count = count / (norm)  # normalize 
        
        return self.bins, count, angles

class vacf(Observable):
    def __init__(self, system, t_range):
        super(vacf, self).__init__(system)
        self.t_window = [i for i in range(1, t_range, 1)]

    def forward(self, vel):
        vacf = [(vel * vel).mean()[None]]
        # can be implemented in parrallel
        vacf += [ (vel[t:] * vel[:-t]).mean()[None] for t in self.t_window]

        return torch.stack(vacf).reshape(-1)

def compute_virial(q, model):
    u = model(q)
    f = -compute_grad(inputs=q, output=u)
    virial = (f * q).sum(-1).sum(-1)
    
    return virial 

def compute_angle(xyz, angle_list, cell, N):
    
    device = xyz.device
    xyz = xyz.reshape(-1, N, 3)
    bond_vec1 = xyz[angle_list[:,0], angle_list[:,1]] - xyz[angle_list[:,0], angle_list[:, 2]]
    bond_vec2 = xyz[angle_list[:,0], angle_list[:,3]] - xyz[angle_list[:,0], angle_list[:, 2]]
    bond_vec1 = bond_vec1 + get_offsets(bond_vec1, cell, device) * cell
    bond_vec2 = bond_vec2 + get_offsets(bond_vec2, cell, device) * cell  
    
    angle_dot = (bond_vec1 * bond_vec2).sum(-1)
    norm = ( bond_vec1.pow(2).sum(-1) * bond_vec2.pow(2).sum(-1) ).sqrt()
    cos = angle_dot / norm
    
    return cos

def compute_dihe(xyz, dihes): 
    assert len(xyz.shape) == 3
    n_frames = xyz.shape[0]
    N = xyz.shape[1]
    xyz = xyz[:, :, None, :]
    D = xyz.expand(n_frames, N,N,3)-xyz.expand(n_frames, N,N,3).transpose(1,2)
    vec1 = D[:, dihes[:,1], dihes[:,0]]
    vec2 = D[:, dihes[:,1], dihes[:,2]]
    vec3 = D[:, dihes[:,2], dihes[:,1]]
    vec4 = D[:, dihes[:,2], dihes[:,3]]
    cross1 = torch.cross(vec1, vec2)
    cross2 = torch.cross(vec3, vec4)

    norm = (cross1.pow(2).sum(-1)*cross2.pow(2).sum(-1)).sqrt()
    cos_phi = 1.0*((cross1*cross2).sum(-1)/norm)
    
    return cos_phi 

def var_K(N_atoms, avg_momentum):
    """compute variances of kinetic energy 
    
    Args:
        N_atoms (TYPE): Description
        avg_momentum (TYPE): Description
    
    Returns:
        TYPE: Description
    """
    return (2 * ((0.5 * 3 * N_atoms * avg_momentum **2 ) ** 2)/(3 * N_atoms) ) ** (1/2)

def plot_ke(v, target_mometum):
    target = 0.5 * Natoms * 3 * (target_mometum **2)
    particle_ke = 0.5 * (v.reshape(-1, Natoms, 3).pow(2) / f_x.mass[:, None])
    sys_ke = particle_ke.sum(-1).sum(-1)
    plt.plot(sys_ke.detach().cpu().numpy())
    plt.plot([i for i in range(sys_ke.shape[0])], [target for i in range(sys_ke.shape[0])] )


# New Observable function packed with data and plotting 

# class Observable(torch.nn.Module):
#     def __init__(self, system, tag, data, loss_func):
#         super(Observable, self).__init__()
#         #check_system(system)
#         self.device = system.device
#         self.volume = system.get_volume()
#         self.cell = torch.Tensor( system.get_cell()).diag().to(self.device)
#         self.natoms = system.get_number_of_atoms()

#         self.tag = tag 
#         self.data = data.to(system.device) # n-dim array
#         self.loss_func = loss_func
#         self.log = []
    
#     def get_loss(self, x):
#         return self.loss_func(self(x), self.data) 
    
#     def update_log(self, results):
#         self.log.append(results.detach().cpu().numpy())
        
#     def save_log(self, path):
#         np.savetxt(path, np.array(self.log))
    
#     def forward(self):
#         pass 
        

# class rdf(Observable):
#     def __init__(self, 
#                  system, 
#                  nbins, 
#                  r_range, 
#                  data,
#                  tag,
#                  loss_func,
#                  index_tuple=None, 
#                  width=None):
        
#         super(rdf, self).__init__(system, tag, data, loss_func)
#         PI = np.pi

#         start = r_range[0]
#         end = r_range[1]
#         self.device = system.device

#         V, vol_bins, bins = generate_vol_bins(start, end, nbins, dim=system.dim)

#         self.V = V
#         self.vol_bins = vol_bins.to(self.device)
#         self.r_axis = np.linspace(start, end, nbins)
#         self.device = system.device
#         self.bins = bins

#         self.smear = GaussianSmearing(
#             start=start,
#             stop=bins[-1],
#             n_gaussians=nbins,
#             width=width,
#             trainable=False
#         ).to(self.device)

#         self.nbins = nbins
#         self.cutoff_boundary = end + 5e-1
#         self.index_tuple = index_tuple
        
#     def forward(self, xyz):

#         nbr_list, pair_dis, _ = generate_nbr_list(xyz, 
#                                                self.cutoff_boundary, 
#                                                self.cell, 
#                                                index_tuple=self.index_tuple, 
#                                                get_dis=True)

#         count = self.smear(pair_dis.reshape(-1).squeeze()[..., None]).sum(0) 
#         norm = count.sum()   # normalization factor for histogram 
#         count = count / norm   # normalize 
#         count = count
#         rdf =  count / (self.vol_bins / self.V ) 
        
#         self.update_log(rdf)
#         return rdf    

# class vacf(Observable):
#     def __init__(self, system, t_range, tag, data, loss_func):
#         super(vacf, self).__init__(system, tag, data, loss_func)
#         self.t_window = [i for i in range(1, t_range, 1)]

#     def forward(self, vel):
#         vacf = [(vel * vel).mean()[None]]
#         # can be implemented in parrallel
#         vacf += [ (vel[t:] * vel[:-t]).mean()[None] for t in self.t_window]
        
#         self.update_log(vacf)

#         return torch.stack(vacf).reshape(-1)
