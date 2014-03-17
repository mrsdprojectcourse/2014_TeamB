#!/usr/bin/env python
from Tkinter import *
from copy import deepcopy
import math
import IPython

#ROS Imports
import roslib
roslib.load_manifest('spacejockey')
import rospy
import sys
from spacejockey.msg import PlannerAction

#get configuration constants from the param server
#this class recursively parses configuration attributes into properties, letting us use the easy . syntax
class ParamNode:
    def __init__(self, **entries): 
        self.__dict__.update(entries)
        for key in self.__dict__:
        	if type(self.__dict__[key]) is dict:
        		self.__dict__[key] = ParamNode(**self.__dict__[key])
config = ParamNode(**rospy.get_param("/planner"))

#interpolation constants
tau = 0.0
dtau = 0.05
ferr = 0.0001 #floating point comparison error correction term
draw_view = False

#major action number for planned moves
major_action_id = 0

#ROS node stuff
actionPub = rospy.Publisher('major_actions', PlannerAction)

class Point:
	"""Point class stores and X,Y pair in meters, and provides scaling to/from the screen size, theta is optional"""
	PX = 1
	M = 2
	def __init__(self, x = 0, y = 0, units = M):
		global config
		if (units == Point.PX):
			self.x = (x - config.window.origin.x) * config.window.scale
			self.y = (config.window.origin.y - y) * config.window.scale
		else:
			self.x = x
			self.y = y

	#returns output values in screen coordinates
	def toScreen(self):
		global config
		n_x = (self.x  / config.window.scale) + config.window.origin.x
		n_y = config.window.origin.y - (self.y / config.window.scale)
		return(n_x, n_y)

	#returns the pythagorean distance between two points
	def distTo(self, other):
		return math.sqrt((self.x-other.x)**2 + (self.y-other.y)**2)

	#returns the global bearing of the other point
	def angleTo(self, other):
		return math.atan2(other.y - self.y, other.x - self.x) 

#returns the difference between two angles
def angleDiff(x, y): 
	return math.atan2(math.sin(x-y), math.cos(x-y))

class AngledPoint(Point):
	def __init__(self, x = 0, y = 0, theta = 0, units = Point.M):
		Point.__init__(self, x, y, units)
		self.theta = theta

	def __repr__(self):
		return '[' + "{:.3f}".format(self.x) + ', ' + "{:.3f}".format(self.y) + ', ' + "{:.4f}".format(self.theta) + ']'

#robot configuration locations are represented as tuples of AngledPoints representing each foot.
nodeNames = ("front_foot", "middle_foot", "rear_foot")
currconfig = (AngledPoint(.05),AngledPoint(),AngledPoint(-.05))
nextconfig = (AngledPoint(.05),AngledPoint(),AngledPoint(-.05))

class Waypoint(Point):
    VIEW = 1
    MOVE = 2

    def __init__(self, x = 0, y = 0, action = MOVE, units = Point.M):
		Point.__init__(self, x,y,units)
		self.action = action

#This is the FIFO queue of waypoints
#implemented with a list
waypoints = []

def isAtWaypoint(rconfig, wp):
	global ferr, config
	if(wp.action == Waypoint.MOVE):
		return rconfig[1].distTo(wp) < ferr
	else:
		return config.view.min > rconfig[1].distTo(wp) < config.view.max
	
#Returns the next major planning action
#Rconfig: Current Configuration
#wp: Waypoint 	
def getNextMove(rconfig, wp):
	global config, major_action_id, ferr

	out = PlannerAction(major_id = major_action_id, action_type = PlannerAction.STEP)
	major_action_id += 1

	tgtAngle = rconfig[1].angleTo(wp)
	tgtDist = rconfig[1].distTo(wp) 

	if wp.action == Waypoint.VIEW: #stop short of view waypoints to get in ideal image range
		tgtDist -= config.view.opt
	
	#output pseudoparams
	extend = config.extend.min  #desired extension
	out.theta = tgtAngle		#init desired angle

	#distances and angle error terms
	frontRel = angleDiff(tgtAngle, rconfig[0].theta) #relative angle between front and target
	backRel = angleDiff(tgtAngle, rconfig[2].theta) #relative angle between back and target
	d1 = rconfig[1].distTo(rconfig[0]) #distance between front and middle
	d2 = rconfig[1].distTo(rconfig[2]) #distance between middle and rear

	if(frontRel > ferr or backRel > ferr): #feet not pointing at the thing
		if(abs(frontRel) >= abs(backRel)): #rotate feet, starting with whichever is farther away
			out.node_name = "front_foot"
			out.theta = rconfig[2].theta + min(backRel, math.copysign(config.angle.max, backRel), key=abs)
		else:
			out.theta = rconfig[0].theta + min(frontRel, math.copysign(config.angle.max, frontRel), key=abs)
			out.node_name = "rear_foot"
			extend = -config.extend.min
	else:									#move towards the thing
		if(wp.action == Waypoint.VIEW and tgtDist < config.view.max):	#view plan action
			out.node_name = "camera"
			out.action_type = PlannerAction.VIEW
		elif (d1 < (config.extend.max - ferr)): #extend front segment, accounting for error
			out.node_name = "front_foot"
			extend = config.extend.max
		elif (d2 > config.extend.min + ferr): #retract rear foot
			out.node_name = "rear_foot"
			extend = -config.extend.min
		else: #extend middle segment
			out.node_name = "middle_foot"
			extend = min(tgtDist, config.extend.max - config.extend.min)

	#derive final joint coordinates
	out.x = rconfig[1].x + (extend * math.cos(out.theta))
	out.y = rconfig[1].y + (extend * math.sin(out.theta))
	return out

#main application class
class App:
	
	def __init__(self, master):
		global config, waypoints
		self.root = master
		frame = Frame(master)
		frame.pack()
		
		self.prevX = 0
		self.prevY = 0

		self.canvas = Canvas(frame, bg="black", height = config.window.height, width = config.window.width, confine = "true", cursor = "cross")
		self.canvas.pack(side=TOP)
		self.canvas.bind("<Button-1>", self.clickCB) 
		self.canvas.bind("<Button-3>", self.clickCB) 
		
		self.status = StringVar()
		self.status.set("IDLE")
		self.statusbox = Label(frame, textvariable = self.status, relief=SUNKEN, width = 20);
		self.statusbox.pack(side=RIGHT)

		#start up ROS
		rospy.init_node('waypoint_planner')
		rospy.loginfo('Waypoint Planner Online')

		self.update()

	def drawPoint(self, p, color = "blue", tag = ""):
		x,y = p.toScreen()
		self.canvas.create_oval(x-3,y-3,x+3,y+3, outline=color, tags=tag)
		if(isinstance(p, AngledPoint)):
			cTh = 6 * math.cos(p.theta)
			sTh = 6 * math.sin(p.theta)
			self.canvas.create_line(x - cTh, y + sTh, x + cTh, y - sTh, fill=color, tags=tag)
	
	def redraw(self):
		#redraw everything
		self.canvas.delete(ALL) 

		#redraw waypoints
		for w in waypoints:
			color = "green" if w.action == Waypoint.MOVE else "red"
			self.drawPoint(w,color,"waypoint")				
			
		#draw Robot config
		for f in currconfig:
			self.drawPoint(f,"blue","foot")


	def update(self):
		global waypoints, currconfig, nextconfig
		
		#update robot movement plan
		if(len(waypoints) == 0):
			self.status.set("Idle")
		else:
			self.status.set("Moving")
			if isAtWaypoint(currconfig, waypoints[0]):
				waypoints.pop(0)
			else:
				currconfig = deepcopy(nextconfig)
				move = getNextMove(currconfig, waypoints[0])
				actionPub.publish(move)

				#update current config from move params
				try:
					i = nodeNames.index(move.node_name)
				except:
					i = 0
				nextconfig[i].x = move.x
				nextconfig[i].y = move.y
				nextconfig[i].theta = move.theta
				nextconfig[1].theta = nextconfig[2].theta
		self.redraw()
		self.root.after(500, self.update)
			
	#on mouse clicks, add a waypoint
	def clickCB(self, event):
		global waypoints
		canvas = event.widget
		x = canvas.canvasx(event.x)
		y = canvas.canvasy(event.y)
		action = Waypoint.MOVE if event.num == 1 else Waypoint.VIEW
		waypoints.append(Waypoint(x, y, action, Point.PX))
		self.redraw()

#set up window
root = Tk()   
root.wm_title("Planner Prototype")
root.config(background="black")
app = App(root)

#start the GUI
root.mainloop()
