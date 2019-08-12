#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <vector>
#include <array>
#include <string>
#include <map>

namespace py = pybind11;

using std::cout;
using std::map;
using std::vector;
using std::string;

float get_intersect_of_lines(vector<map<string, vector<float>>> py_lines){
    int line_num = py_lines.size();
    int count = 0;
    float unit = 0;
    float point = 0;
    array<array<int, 2>, line_num> lines;

    for (auto line : py_lines){
        unit = line.find("unit")->second;
        point = line.find("point")->second;
        lines[count][0] = point;
        lines[count][1] = unit;
    }
}

PYBIND11_MODULE(ray_trace, m){
    m.doc() = "refractive ray tracing";
    m.def("get_intersect_of_lines", &get_intersect_of_lines,
          "calculate the point that are closest to multipl lines");
}